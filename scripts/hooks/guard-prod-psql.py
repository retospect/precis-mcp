#!/usr/bin/env -S uv run --no-project --python >=3.12 python
"""PreToolUse hook: auto-approve read-only ``scripts/prod-psql`` probes, ask on writes.

The friction this removes: routine prod polling (`scripts/prod-psql "SELECT …"`)
is the read-only way to peek at production, but `prod-psql` connects as
**write-capable `agent_rw`** and has no allow-list entry, so every SELECT
prompts. Blanket-allowing `Bash(scripts/prod-psql:*)` would fix the friction
but silently auto-approve `UPDATE`/`DELETE` on PROD too — the opposite of safe.

So this hook is the single decision point, keyed off the SQL:
- A **lone** `scripts/prod-psql --ro "<any SQL>"` → **allow**: it runs as
  agent_ro inside BEGIN READ ONLY, so the server refuses writes.
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


#: A heredoc opener (``<<EOF``, ``<<-'EOF'``, ``<<"EOF"``); its body is data.
_HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")

#: The prod-psql executable as a command word (bare or by path).
_PROD_PSQL_WORD = re.compile(r"^(?:\S*/)?scripts/prod-psql$")

#: An env assignment prefix (``FOO=bar``) before the command word.
_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=\S*$")


def _strip_heredocs(command: str) -> str:
    """Drop every heredoc BODY (the opener line stays). A heredoc is data —
    a design note appended with ``cat >> note <<'EOF'`` that merely mentions
    ``scripts/prod-psql`` or the word DELETE is not a prod write."""
    lines = command.split("\n")
    out: list[str] = []
    pending: list[str] = []
    for line in lines:
        if pending:
            if line.strip() == pending[0]:
                pending.pop(0)
            continue
        out.append(line)
        pending.extend(m.group(2) for m in _HEREDOC.finditer(line))
    return "\n".join(out)


def _segments(command: str) -> list[tuple[str, str]]:
    """Split on ``&&``, ``||``, ``;``, ``|`` and newlines OUTSIDE quotes.
    Returns ``(text, joiner_before)`` pairs so a pipe into prod-psql is
    recognisable."""
    segs: list[tuple[str, str]] = []
    buf: list[str] = []
    quote = ""
    joiner = ""
    i = 0
    while i < len(command):
        c = command[i]
        if quote:
            buf.append(c)
            if c == quote:
                quote = ""
            elif c == "\\" and quote == '"' and i + 1 < len(command):
                buf.append(command[i + 1])
                i += 1
        elif c in "'\"":
            quote = c
            buf.append(c)
        elif command.startswith(("&&", "||"), i):
            segs.append(("".join(buf), joiner))
            buf, joiner = [], command[i : i + 2]
            i += 1
        elif c in ";|\n":
            segs.append(("".join(buf), joiner))
            buf, joiner = [], c
        else:
            buf.append(c)
        i += 1
    segs.append(("".join(buf), joiner))
    return [(t, j) for t, j in segs if t.strip()]


def _invocation(segment: str) -> tuple[bool, str] | None:
    """``(ro, args_text)`` when ``segment`` RUNS prod-psql — its command word,
    after any ``FOO=bar`` prefixes, is scripts/prod-psql. ``None`` when the
    name only appears as an argument (a grep pattern, a commit message)."""
    words = segment.strip().split(None, 1)
    while words and _ENV_ASSIGN.match(words[0]):
        words = words[1].split(None, 1) if len(words) > 1 else []
    if not words or not _PROD_PSQL_WORD.match(words[0]):
        return None
    rest = words[1] if len(words) > 1 else ""
    ro = False
    if rest.startswith("--ro"):
        ro, rest = True, rest[len("--ro") :].lstrip()
    return ro, rest


def evaluate(command: str) -> dict[str, str] | None:
    """Return a ``{decision, reason}`` dict, or ``None`` for no opinion.

    Pure & testable — no I/O, no env reads (the ALLOW_PROD_WRITE escape hatch is
    applied by ``main`` so the read-only allow can't be switched off).

    Only a segment that RUNS prod-psql counts, heredoc bodies are ignored, and
    write keywords are looked for only in what prod-psql receives (its SQL
    argument, or the pipeline feeding its stdin) — the hook used to fire on
    any command that merely mentioned the script or a word like DELETE.
    """
    if "prod-psql" not in command:
        return None
    segs = _segments(_strip_heredocs(command))
    calls = [(n, inv) for n, (t, _j) in enumerate(segs) if (inv := _invocation(t))]
    if not calls:
        return None

    if len(segs) == 1:
        ro, args = calls[0][1]
        m = re.fullmatch(r"\s*(\"[^\"]*\"|'[^']*')\s*", args)
        if m:
            if ro:
                # agent_ro inside BEGIN READ ONLY: the server refuses any
                # write, and the script refuses a session-level SET.
                return {
                    "decision": "allow",
                    "reason": "prod-psql --ro (agent_ro, read-only transaction) — auto-approved",
                }
            if _is_readonly_sql(m.group(1)):
                return {
                    "decision": "allow",
                    "reason": "read-only prod-psql probe (SELECT/EXPLAIN/backslash) — auto-approved",
                }

    # Not a provable read-only lone call: ask only if what prod-psql actually
    # receives carries a write keyword; otherwise stay silent (normal prompt).
    for n, (ro, args) in calls:
        fed = args
        k = n
        while k > 0 and segs[k][1] == "|":  # stdin piped in from the left
            k -= 1
            fed += " " + segs[k][0]
        if not ro and _WRITE_KEYWORDS.search(fed):
            return {
                "decision": "ask",
                "reason": (
                    "`scripts/prod-psql` with a write/side-effecting statement targets "
                    "**PRODUCTION** (`precis_prod`, agent_rw). Prefer `--ro` for reads; "
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

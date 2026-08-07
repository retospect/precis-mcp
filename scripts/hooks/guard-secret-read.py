#!/usr/bin/env python3
"""PreToolUse hook: DENY reads of files that hold cleartext credentials.

Why a gate and not a nudge (unlike ``guard-prod-write.py``, which warns and
allows): reading a secret is **irreversible**. Once the bytes are in an agent
context they are in the transcript, in any summary of it, and in any subagent
that inherits it — there is no unread. A warning after the fact protects
nothing, so this one denies.

The incident: ``/Users/deploy/.claude/mcp.json`` on the scheduler node carried
``ACATOME_PG_PASSWORD`` (the `agent_rw` prod password) in cleartext, and a
session ``cat``-ed it whole while investigating an unrelated MCP-connect
question — twice. The file itself is fixed (the dead ACATOME_PG_* block is gone
from ``deploy/roles/asa_bot/templates/claude_mcp.json.j2``), but the *class* of
mistake isn't: a config file is the natural thing to read when debugging a
config problem, and the secret is incidental to what you actually wanted.

Denial is **path-shaped, not content-shaped** — the hook cannot see the file, so
it matches on names that hold secrets by convention. It fires on Read and on the
Bash readers (``cat``/``head``/``tail``/``less``/``strings``/``xxd``); a
redacting pipeline (``sed``/``grep``/``jq``/``rg``) is allowed through, since
that is the sanctioned way to inspect one of these files:

    sed -E 's#(//[^:]*:)[^@]*@#\\1***@#g' <file>     # DSN passwords
    jq 'del(.. | .env?)' <file>                      # drop env blocks

Editing / templating a secret file is untouched — this guards *reading* only,
and a Jinja template (``*.j2``) holds ``{{ vault_* }}`` placeholders, not
secrets, so templates are explicitly NOT matched.

Escape hatch: ``ALLOW_SECRET_READ=1`` for a deliberate, eyes-open read (a
rotation, say). Prefer redaction; reach for the hatch only when you genuinely
need the value and know where it will end up.

Wired in ``.claude/settings.json`` (PreToolUse, matchers ``Read`` and ``Bash``).
"""

from __future__ import annotations

import json
import os
import re
import sys

# Filenames/paths that hold cleartext credentials by convention. Matched against
# the whole path, case-insensitively.
SECRET_PATHS = (
    r"\.claude/mcp\.json$",  # MCP env blocks — the incident above
    r"\.vault-pass$",  # ansible vault password
    r"/\.secrets/",  # ~/.secrets/pw/* — the repo's own creds dir
    r"\.pgpass$",  # libpq password file
    r"\.netrc$",
    r"\.env(\.[\w-]+)?$",  # .env, .env.local, .env.prod
    r"/(id_rsa|id_ed25519|id_ecdsa)$",  # private keys (the .pub sibling is fine)
    r"credentials(\.json)?$",
    r"\.claude_oauth_token$",  # asa's long-lived token (see memory runbook)
)

# Bash commands that dump a file wholesale. A redacting/filtering reader
# (sed/grep/jq/rg/awk) is deliberately absent — that is the sanctioned path.
DUMPERS = ("cat", "head", "tail", "less", "more", "bat", "strings", "xxd", "od")


def _is_secret(path: str) -> bool:
    """Does this path name a file that holds cleartext credentials?"""
    if not path or path.endswith(".j2"):  # a template holds {{ vault_* }}, not a secret
        return False
    return any(re.search(p, path, re.IGNORECASE) for p in SECRET_PATHS)


def _bash_targets(command: str) -> list[str]:
    """Paths a wholesale-dumper reads in ``command`` (redacting readers ignored).

    Deliberately crude: split on the shell operators that start a new command,
    then look at segments whose first word is a dumper. A `cat secret | sed …`
    still trips — the dump happens before the filter, so the bytes are already
    in the pipe. `sed … secret` does not, which is the point.
    """
    out: list[str] = []
    for seg in re.split(r"[|;&\n]+|\$\(|`", command):
        words = seg.strip().split()
        if not words:
            continue
        cmd = words[0].rsplit("/", 1)[-1]
        if cmd in DUMPERS:
            out += [w for w in words[1:] if not w.startswith("-")]
    return out


def evaluate(tool_name: str, tool_input: dict) -> str | None:
    """Return a denial reason, or ``None`` to allow. Pure & testable."""
    ti = tool_input or {}
    if tool_name == "Read":
        hits = [ti.get("file_path", "")] if _is_secret(ti.get("file_path", "")) else []
    elif tool_name == "Bash":
        hits = [p for p in _bash_targets(ti.get("command", "")) if _is_secret(p)]
    else:
        return None
    if not hits:
        return None
    return (
        f"🔒 Refusing to read {', '.join(hits)} — it holds a cleartext credential, "
        "and a secret read into an agent context cannot be un-read (it persists in "
        "the transcript, its summaries, and any subagent that inherits them).\n"
        "Inspect it redacted instead, e.g.\n"
        "    sed -E 's#(//[^:]*:)[^@]*@#\\1***@#g' <file>\n"
        "    jq 'del(.. | .env?)' <file>\n"
        "If you genuinely need the value (a rotation), set ALLOW_SECRET_READ=1."
    )


def main() -> int:
    if os.environ.get("ALLOW_SECRET_READ"):
        return 0
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    reason = evaluate(payload.get("tool_name", ""), payload.get("tool_input") or {})
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

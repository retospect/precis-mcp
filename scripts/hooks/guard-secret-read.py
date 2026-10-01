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

It also denies the commands that print a container's environment
(``docker inspect`` without a narrowing ``--format``, ``docker exec <c> env``,
``/proc/<pid>/environ``): a container launched with ``-e`` secrets holds them
there in cleartext, and on 2026-09-30 an inspect meant to read one flag put
the prod DSN into a transcript (gr458350).

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
    r"^/proc/[^/]+/environ$",  # a process's env — where -e secrets end up
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


#: A ``--format`` that prints env values: ``.Config.Env`` itself (unless it is
#: split down to names), the whole ``.Config``, or the whole object.
_ENV_FORMAT = re.compile(
    r"\.Config\.Env|\.Config\s*\}\}|\{\{\s*(json\s+)?\.\s*\}\}"
)


def _docker_env_dumps(command: str) -> list[str]:
    """``docker`` invocations in ``command`` that print a container's env values.

    ``docker inspect`` with no ``--format`` prints ``.Config.Env`` along with
    everything else, so it counts; a ``--format`` naming another field does
    not. ``docker exec <c> env``/``printenv`` prints the same values from
    inside. Nothing about these commands looks like a credential read, which
    is how one put the prod DSN into a transcript on 2026-09-30 (gr458350).
    """
    out: list[str] = []
    for seg in re.split(r"[|;&\n]+|\$\(|`", command):
        text = seg.strip()
        words = text.split()
        if len(words) < 2 or words[0].rsplit("/", 1)[-1] != "docker":
            continue
        sub = words[2:] if words[1] == "container" else words[1:]
        if not sub:
            continue
        if sub[0] == "inspect":
            if len(sub) < 2:  # no target: prose naming the command, not a call
                continue
            has_format = re.search(r"(^|\s)(--format|-f)(=|\s)", text)
            if not has_format or (_ENV_FORMAT.search(text) and "split" not in text):
                out.append(text)
        elif sub[0] == "exec" and {"env", "printenv"} & set(sub[1:]):
            out.append(text)
    return out


def evaluate(tool_name: str, tool_input: dict) -> str | None:
    """Return a denial reason, or ``None`` to allow. Pure & testable."""
    ti = tool_input or {}
    if tool_name == "Read":
        hits = [ti.get("file_path", "")] if _is_secret(ti.get("file_path", "")) else []
    elif tool_name == "Bash":
        command = ti.get("command", "")
        hits = [p for p in _bash_targets(command) if _is_secret(p)]
        env_dumps = _docker_env_dumps(command)
        if env_dumps and not hits:
            return (
                f"🔒 Refusing `{env_dumps[0]}` — it prints a container's environment, "
                "and a container launched with `-e` secrets carries the prod DSN and "
                "API keys there in cleartext (gr458350). A secret read into an agent "
                "context cannot be un-read.\n"
                "Read the variable NAMES instead:\n"
                "    docker inspect <c> --format "
                "'{{range .Config.Env}}{{println (index (split . \"=\") 0)}}{{end}}'\n"
                "or ask for the specific field you want (--format '{{json .Mounts}}'). "
                "If you genuinely need the values, set ALLOW_SECRET_READ=1."
            )
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

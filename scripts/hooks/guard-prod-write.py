#!/usr/bin/env python3
"""PreToolUse hook: confirm before a session-MCP WRITE mutates PROD.

The footgun this closes: the precis MCP loaded in a dev session is the local
"5th worker" whose DB-backed kinds (todo, gripe, memory, paper, quest, …) target
**`precis_prod`** as `agent_rw` (WRITE-capable). Dogfooding is meant to be
read-only (`search`/`get`/`more`); `put`/`edit`/`delete`/`tag` on a DB kind
silently mutate production. This surfaces that as an **ask** (confirm), so a
real prod write is deliberate, not accidental.

Keyed off the TARGET, not just the verb, to stay quiet on legitimate work:
- **File-kinds** (`markdown`/`plaintext`/`tex`) are sandboxed to `PRECIS_ROOT`,
  not the DB → allowed silently.
- A dev-DB session (``PRECIS_DATABASE_URL`` → `precis_test` / localhost) → allowed.
- Everything else (a DB-backed kind on the prod session MCP) → **ask**.

Escape hatch: ``ALLOW_PROD_WRITE=1`` for a session of intentional prod writes.
For write-PATH testing, drive a dev-DB precis (`scripts/dev`), not the session MCP.

Wired in ``.claude/settings.json`` (PreToolUse, matcher
``mcp__precis__put|mcp__precis__edit|mcp__precis__delete|mcp__precis__tag``).
"""

from __future__ import annotations

import json
import os
import re
import sys

FILE_KINDS = {"markdown", "plaintext", "tex"}  # sandboxed to PRECIS_ROOT, not the DB


def _dev_dsn() -> bool:
    """True if the session's DSN clearly points at a dev/test DB (not prod)."""
    dsn = os.environ.get("PRECIS_DATABASE_URL", "")
    return bool(re.search(r"precis_test|localhost|127\.0\.0\.1:5432", dsn))


def evaluate(tool_name: str, tool_input: dict) -> str | None:
    """Return an ask reason, or ``None`` to allow. Pure & testable."""
    if not isinstance(tool_name, str) or not tool_name.startswith("mcp__precis__"):
        return None
    verb = tool_name.split("__")[-1]
    if verb not in {"put", "edit", "delete", "tag"}:
        return None
    kind = (tool_input or {}).get("kind")
    if kind in FILE_KINDS:  # sandbox file write, not prod
        return None
    if _dev_dsn():  # a dev-DB session MCP
        return None
    return (
        f"`{verb}(kind={kind!r})` via the session precis MCP is a WRITE to "
        "**PROD** (`precis_prod`, agent_rw). Dogfooding is read-only "
        "(search/get/more); for write-path testing drive a dev-DB precis "
        "(`scripts/dev`). Proceed only for a deliberate prod mutation "
        "(e.g. a gripe / todo / memory). Set ALLOW_PROD_WRITE=1 to stop asking."
    )


def main() -> int:
    if os.environ.get("ALLOW_PROD_WRITE"):
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
                    "permissionDecision": "ask",
                    "permissionDecisionReason": reason,
                }
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

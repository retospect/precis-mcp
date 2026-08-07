"""Regression tests for ``scripts/hooks/guard-secret-read.py``.

The incident (2026-08-07): ``/Users/deploy/.claude/mcp.json`` on the scheduler
node carried the `agent_rw` prod password in cleartext via a dead
``ACATOME_PG_PASSWORD`` var, and a session read the file whole — twice — while
debugging an unrelated MCP-connect question. The file is fixed (that block is
gone from ``deploy/roles/asa_bot/templates/claude_mcp.json.j2``); this pins the
guard that stops the *class* of mistake, since a secret read into an agent
context cannot be un-read.

Two properties matter and they pull against each other:

* a wholesale dump of a secret-by-convention path is **denied** (not warned —
  the damage is done by the time a warning is read), and
* the redacting readers stay usable, or the guard just teaches people to reach
  for ``ALLOW_SECRET_READ=1`` and the gate is worse than nothing.

Pure-function tests against ``evaluate()`` plus one end-to-end run of the real
script over stdin, so the JSON contract with the harness is covered too.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

HOOK = (
    Path(__file__).resolve().parents[1] / "scripts" / "hooks" / "guard-secret-read.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("guard_secret_read", HOOK)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


guard = _load()


@pytest.mark.parametrize(
    "path",
    [
        "/Users/deploy/.claude/mcp.json",  # the incident itself
        "/Users/reto/work/deploy/inventory/.vault-pass",
        "/Users/reto/.secrets/pw/PRECIS_TEST_PG_URL",
        "/Users/deploy/.pgpass",
        "/home/deploy/.env.prod",
        "/Users/reto/.ssh/id_ed25519",
        "/Users/hermes/.claude_oauth_token",
    ],
)
def test_read_of_a_secret_path_is_denied(path: str) -> None:
    reason = guard.evaluate("Read", {"file_path": path})
    assert reason is not None
    assert "cannot be un-read" in reason


@pytest.mark.parametrize(
    "path",
    [
        "deploy/roles/asa_bot/templates/claude_mcp.json.j2",  # {{ vault_* }}, not a secret
        "/Users/reto/.ssh/id_ed25519.pub",  # public half
        "src/precis/utils/llm/router.py",
        "OPEN-ITEMS.md",
        "/Users/reto/.claude/settings.json",  # config, no creds
    ],
)
def test_ordinary_reads_are_untouched(path: str) -> None:
    assert guard.evaluate("Read", {"file_path": path}) is None


def test_j2_template_is_never_a_secret_even_when_named_like_one() -> None:
    """A template holds ``{{ vault_pg_agent_rw_pass }}`` — a placeholder.

    Guarding it would block editing the very file that fixes a leak.
    """
    assert guard.evaluate("Read", {"file_path": "roles/x/templates/.pgpass.j2"}) is None


@pytest.mark.parametrize(
    "command",
    [
        "cat /Users/deploy/.claude/mcp.json",
        "head -20 /Users/deploy/.claude/mcp.json",
        "ssh melchior 'true'; cat ~/.vault-pass",
        "cat /Users/deploy/.claude/mcp.json | sed 's/x/y/'",  # dump-then-filter still leaks
        "xxd /Users/deploy/.pgpass",
    ],
)
def test_bash_wholesale_dump_is_denied(command: str) -> None:
    assert guard.evaluate("Bash", {"command": command}) is not None


@pytest.mark.parametrize(
    "command",
    [
        # the sanctioned redacting reads — these MUST keep working
        "sed -E 's#(//[^:]*:)[^@]*@#\\1***@#g' /Users/deploy/.claude/mcp.json",
        "jq 'del(.. | .env?)' /Users/deploy/.claude/mcp.json",
        "grep -c mcpServers /Users/deploy/.claude/mcp.json",
        "ls -l /Users/deploy/.claude/mcp.json",  # metadata, not content
        "cat src/precis/utils/llm/router.py",  # ordinary file
        "test -f /Users/deploy/.pgpass && echo present",
    ],
)
def test_redacting_and_metadata_reads_are_allowed(command: str) -> None:
    assert guard.evaluate("Bash", {"command": command}) is None


def test_other_tools_are_ignored() -> None:
    """Editing/writing a secret file is out of scope — this guards reads."""
    assert guard.evaluate("Edit", {"file_path": "/Users/deploy/.pgpass"}) is None


def test_end_to_end_emits_a_deny_decision() -> None:
    proc = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(
            {"tool_name": "Read", "tool_input": {"file_path": "/x/.claude/mcp.json"}}
        ),
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin"},  # no ALLOW_SECRET_READ
    )
    assert proc.returncode == 0
    out = json.loads(proc.stdout)["hookSpecificOutput"]
    assert out["hookEventName"] == "PreToolUse"
    assert out["permissionDecision"] == "deny"


def test_escape_hatch_allows_the_read() -> None:
    proc = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(
            {"tool_name": "Read", "tool_input": {"file_path": "/x/.claude/mcp.json"}}
        ),
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "ALLOW_SECRET_READ": "1"},
    )
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""

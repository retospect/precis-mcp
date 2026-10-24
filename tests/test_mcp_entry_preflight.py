"""scripts/mcp-entry-preflight: re-add the user-scope precis MCP entry, idempotently.

The client registry entry vanished while the server stayed healthy and the
fleet ran blind (gr513664, 2026-10-24). The preflight goes through
``claude mcp`` only — a fake ``claude`` and a fake ensure script on PATH /
PRECIS_MCP_ENSURE keep the real registry and server out of the test.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX-only: runs the script under bash"
)

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "mcp-entry-preflight"

FAKE_CLAUDE = """#!/usr/bin/env bash
# Registry state = a flag file; `get` fails without it, `add-json` creates it.
echo "$*" >> "$FAKE_CLAUDE_CALLS"
if [ "$1 $2" = "mcp get" ]; then
    if [ -f "$FAKE_CLAUDE_STATE" ]; then exit 0; fi
    echo 'No MCP server named "precis".' >&2
    exit 1
fi
if [ "$1 $2" = "mcp add-json" ]; then
    touch "$FAKE_CLAUDE_STATE"
    exit 0
fi
exit 1
"""

FAKE_ENSURE = """#!/usr/bin/env bash
echo called >> "$FAKE_ENSURE_CALLS"
printf '%s\\n' '{"type":"http","url":"http://127.0.0.1:9999/mcp","headers":{"Authorization":"Bearer TESTTOKEN"}}'
"""


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)
    return path


@pytest.fixture
def rig(tmp_path: Path) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write(bin_dir / "claude", FAKE_CLAUDE)
    ensure = _write(tmp_path / "ensure.sh", FAKE_ENSURE)
    return {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "FAKE_CLAUDE_STATE": str(tmp_path / "entry-present"),
        "FAKE_CLAUDE_CALLS": str(tmp_path / "claude-calls"),
        "FAKE_ENSURE_CALLS": str(tmp_path / "ensure-calls"),
        "PRECIS_MCP_ENSURE": str(ensure),
        # Belt and braces: never the real user config.
        "HOME": str(tmp_path),
    }


def _run(rig: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        env={**os.environ, **rig},
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )


def _calls(rig: dict[str, str], name: str) -> list[str]:
    f = Path(rig[name])
    return f.read_text(encoding="utf-8").splitlines() if f.exists() else []


def test_present_entry_is_a_silent_noop(rig: dict[str, str]) -> None:
    Path(rig["FAKE_CLAUDE_STATE"]).touch()
    result = _run(rig)
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert _calls(rig, "FAKE_CLAUDE_CALLS") == ["mcp get precis"]
    assert _calls(rig, "FAKE_ENSURE_CALLS") == []


def test_dry_run_present_reports_nothing_to_do(rig: dict[str, str]) -> None:
    Path(rig["FAKE_CLAUDE_STATE"]).touch()
    result = _run(rig, "--dry-run")
    assert result.returncode == 0
    assert "nothing to do" in result.stdout


def test_missing_entry_is_re_added_at_user_scope(rig: dict[str, str]) -> None:
    result = _run(rig)
    assert result.returncode == 0, result.stderr
    calls = _calls(rig, "FAKE_CLAUDE_CALLS")
    add = [c for c in calls if c.startswith("mcp add-json")]
    assert len(add) == 1
    assert add[0].startswith("mcp add-json -s user precis ")
    assert "http://127.0.0.1:9999/mcp" in result.stdout
    # The config carries the Bearer token; the report must not leak it.
    assert "TESTTOKEN" not in result.stdout + result.stderr


def test_re_add_is_idempotent(rig: dict[str, str]) -> None:
    assert _run(rig).returncode == 0
    second = _run(rig)
    assert second.returncode == 0
    assert second.stdout == ""
    adds = [c for c in _calls(rig, "FAKE_CLAUDE_CALLS") if c.startswith("mcp add-json")]
    assert len(adds) == 1


def test_dry_run_missing_changes_nothing(rig: dict[str, str]) -> None:
    result = _run(rig, "--dry-run")
    assert result.returncode == 0
    assert "would run" in result.stdout
    assert "claude mcp add-json -s user" in result.stdout
    # Nothing executed: no add-json call, no registry state, ensure untouched.
    assert _calls(rig, "FAKE_CLAUDE_CALLS") == ["mcp get precis"]
    assert _calls(rig, "FAKE_ENSURE_CALLS") == []
    assert not Path(rig["FAKE_CLAUDE_STATE"]).exists()


def test_falls_back_to_the_repo_ensure_script(
    rig: dict[str, str], tmp_path: Path
) -> None:
    # PRECIS_MCP_ENSURE pointing nowhere falls back to the repo copy next to
    # the script (deploy/mcp-http/) rather than failing.
    rig["PRECIS_MCP_ENSURE"] = str(tmp_path / "absent.sh")
    result = _run(rig)
    assert result.returncode == 0, result.stderr
    adds = [c for c in _calls(rig, "FAKE_CLAUDE_CALLS") if c.startswith("mcp add-json")]
    assert len(adds) == 1


def test_no_claude_cli_refuses(rig: dict[str, str], tmp_path: Path) -> None:
    # The dev image ships a real `claude`, so the PRECIS_MCP_CLAUDE seam —
    # not PATH — takes the registry command away.
    rig["PRECIS_MCP_CLAUDE"] = str(tmp_path / "no-such-claude")
    result = _run(rig)
    assert result.returncode == 2
    assert "cannot check the MCP registry" in result.stderr


def test_usage_error(rig: dict[str, str]) -> None:
    result = _run(rig, "--bogus")
    assert result.returncode == 2
    assert "usage" in result.stderr

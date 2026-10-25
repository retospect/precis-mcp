"""scripts/mcp-entry-preflight: re-add the user-scope precis MCP entry, idempotently.

The client registry entry vanished while the server stayed healthy and the
fleet ran blind (gr513664, 2026-10-24). The preflight goes through
``claude mcp`` only — a fake ``claude`` and a fake ensure script on PATH /
PRECIS_MCP_ENSURE keep the real registry and server out of the test. The
``--for-hook`` block covers the SessionStart form: heal-or-warn in one
line, never a failure, naming the restart runbook.
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
    # Why PRECIS_MCP_CLAUDE and not a PATH override (gr515182): the dev
    # image's real `claude` is an `npm install -g` binary in /usr/bin
    # (NodeSource's global prefix is /usr), and bookworm's merged-/usr
    # makes /bin the same directory. The rig's PATH must keep /usr/bin —
    # the script under test resolves `bash`, `sed` and friends there —
    # so no PATH that can still run the script also hides `claude`.
    # PRECIS_MCP_CLAUDE naming a nonexistent binary is the supported way
    # to take the registry command away.
    rig["PRECIS_MCP_CLAUDE"] = str(tmp_path / "no-such-claude")
    result = _run(rig)
    assert result.returncode == 2
    assert "cannot check the MCP registry" in result.stderr


def test_usage_error(rig: dict[str, str]) -> None:
    result = _run(rig, "--bogus")
    assert result.returncode == 2
    assert "usage" in result.stderr


# --- --for-hook (the SessionStart form, gr513664) -----------------------------


def test_for_hook_present_entry_is_silent(rig: dict[str, str]) -> None:
    Path(rig["FAKE_CLAUDE_STATE"]).touch()
    result = _run(rig, "--for-hook")
    assert result.returncode == 0
    assert result.stdout == ""


def test_for_hook_heals_and_names_the_runbook(rig: dict[str, str]) -> None:
    result = _run(rig, "--for-hook")
    assert result.returncode == 0, result.stderr
    # Exactly one line: healed, and the restart runbook named.
    lines = result.stdout.strip().splitlines()
    assert len(lines) == 1
    assert "re-added" in lines[0]
    assert "docs/runbooks/session-restart.md" in lines[0]
    assert Path(rig["FAKE_CLAUDE_STATE"]).exists()
    assert "TESTTOKEN" not in result.stdout + result.stderr


def test_for_hook_heal_failure_still_exits_zero(rig: dict[str, str]) -> None:
    ensure = _write(
        Path(rig["FAKE_ENSURE_CALLS"]).parent / "ensure-fail.sh",
        "#!/usr/bin/env bash\nexit 1\n",
    )
    rig["PRECIS_MCP_ENSURE"] = str(ensure)
    result = _run(rig, "--for-hook")
    assert result.returncode == 0
    lines = result.stdout.strip().splitlines()
    assert len(lines) == 1
    assert "MISSING" in lines[0]
    assert "docs/runbooks/session-restart.md" in lines[0]


def test_for_hook_no_ensure_script_warns_once(rig: dict[str, str]) -> None:
    # Both candidates absent: PRECIS_MCP_ENSURE unset falls to a HOME path
    # (the rig's HOME is the empty tmp_path), and the repo-copy fallback is
    # avoided by invoking a COPY of the script outside the repo tree.
    rig.pop("PRECIS_MCP_ENSURE")
    copy = _write(
        Path(rig["FAKE_ENSURE_CALLS"]).parent / "mcp-entry-preflight",
        SCRIPT.read_text(encoding="utf-8"),
    )
    result = subprocess.run(
        ["bash", str(copy), "--for-hook"],
        env={**os.environ, **rig},
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )
    assert result.returncode == 0
    lines = result.stdout.strip().splitlines()
    assert len(lines) == 1
    assert "MISSING" in lines[0]
    assert "docs/runbooks/session-restart.md" in lines[0]


def test_for_hook_no_claude_cli_is_silent(rig: dict[str, str], tmp_path: Path) -> None:
    # No registry to check is not a missing entry: a non-claude harness
    # would otherwise false-warn every session start.
    rig["PRECIS_MCP_CLAUDE"] = str(tmp_path / "no-such-claude")
    result = _run(rig, "--for-hook")
    assert result.returncode == 0
    assert result.stdout == ""


def test_http_install_for_hook_runs_the_preflight(rig: dict[str, str]) -> None:
    # scripts/mcp-http-install --for-hook is the wired SessionStart check;
    # it must hand off to the entry preflight. PRECIS_MCP_ENSURE pointing
    # at the repo copy keeps its own drift check silent, so only the
    # preflight's heal line shows.
    rig["PRECIS_MCP_ENSURE"] = str(
        SCRIPT.parents[1] / "deploy" / "mcp-http" / "precis-mcp-http-ensure.sh"
    )
    result = subprocess.run(
        ["bash", str(SCRIPT.parents[1] / "scripts" / "mcp-http-install"), "--for-hook"],
        env={**os.environ, **rig},
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )
    assert result.returncode == 0
    assert "re-added" in result.stdout
    assert "docs/runbooks/session-restart.md" in result.stdout

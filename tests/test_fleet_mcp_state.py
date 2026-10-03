"""scripts/lib/fleet_mcp_state.py: the `mcp` column of `scripts/fleet status` (gr462596)."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "lib" / "fleet_mcp_state.py"

# Lines copied from Claude Code v2.1.285 client MCP logs on 2026-10-02.
GAVE_UP = "Max reconnection attempts (5) reached, giving up"
STARTUP_LAST_RETRY = "Transient ECONNREFUSED on initial connect — retry 3/3 in 4000ms"
CONNECTED = 'Connection established with capabilities: {"hasTools":true}'
TOOL_OK = "Tool 'get' completed successfully in 8s"
SESSION_EXPIRED = 'Error POSTing to endpoint: {"jsonrpc":"2.0","error":{"message":"Session not found"}}'
STDIO_GONE = "Connection failed after 5ms: MCP error -32000: Connection closed"
RECONNECTED = "HTTP reconnection successful after 812ms (attempt 2)"


def _module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("fleet_mcp_state", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _session(sessions: Path, pid: int, sid: str, pane: str | None) -> None:
    rec = {"pid": pid, "sessionId": sid, "startedAt": 1_000_000}
    if pane:
        rec["tmux"] = f"0:@{pid}.{pane}"
    (sessions / f"{pid}.json").write_text(json.dumps(rec), encoding="utf-8")


def _log(
    cache: Path,
    project: str,
    name: str,
    events: list[tuple[str, str, str]],
    server: str = "precis",
) -> Path:
    d = cache / project / f"mcp-logs-{server}"
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{name}.jsonl"
    f.write_text(
        "".join(
            json.dumps({"debug": t, "timestamp": ts, "sessionId": sid}) + "\n"
            for ts, sid, t in events
        ),
        encoding="utf-8",
    )
    return f


@pytest.fixture
def dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    sessions, cache = tmp_path / "sessions", tmp_path / "cache"
    sessions.mkdir()
    cache.mkdir()
    monkeypatch.setenv("PRECIS_FLEET_SESSIONS_DIR", str(sessions))
    monkeypatch.setenv("PRECIS_FLEET_CLIENT_CACHE", str(cache))
    return sessions, cache


def _run(capsys: pytest.CaptureFixture[str]) -> dict[str, str]:
    assert _module().main([]) == 0
    return dict(line.split() for line in capsys.readouterr().out.splitlines())


def test_last_decisive_line_wins(
    dirs: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    sessions, cache = dirs
    _session(sessions, 1, "gave-up", "%1")
    _session(sessions, 2, "reconnected", "%2")
    _session(sessions, 3, "startup-refused", "%3")
    _session(sessions, 4, "quiet", "%4")
    _session(sessions, 5, "no-tmux", None)
    _log(
        cache,
        "-a",
        "s1",
        [
            ("2026-10-02T14:00:00Z", "gave-up", TOOL_OK),
            ("2026-10-02T15:04:29Z", "gave-up", GAVE_UP),
            ("2026-10-02T14:00:00Z", "reconnected", CONNECTED),
            ("2026-10-02T15:04:29Z", "reconnected", GAVE_UP),
            ("2026-10-02T20:40:00Z", "reconnected", CONNECTED),
        ],
    )
    # A session's lines can sit in another project's log dir (cwd at start).
    _log(
        cache,
        "-b",
        "s2",
        [("2026-10-02T11:34:24Z", "startup-refused", STARTUP_LAST_RETRY)],
    )
    assert _run(capsys) == {"%1": "DOWN", "%2": "ok", "%3": "DOWN", "%4": "-"}


def test_a_log_older_than_every_session_is_not_read(
    dirs: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    sessions, cache = dirs
    _session(sessions, 1, "s", "%1")
    old = _log(cache, "-a", "old", [("2026-10-01T00:00:00Z", "s", GAVE_UP)])
    os.utime(old, (10, 10))  # before startedAt (1_000 s)
    assert _run(capsys) == {"%1": "-"}


def test_runs_as_a_script_with_nothing_to_report(dirs: tuple[Path, Path]) -> None:
    r = subprocess.run(
        [sys.executable, str(SCRIPT)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert (r.returncode, r.stdout) == (0, "")


def test_expired_session_and_gone_stdio_child_are_down_until_reconnected(
    dirs: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    sessions, cache = dirs
    _session(sessions, 1, "expired", "%1")
    _session(sessions, 2, "healed", "%2")
    _session(sessions, 3, "stdio", "%3")
    _log(
        cache,
        "-a",
        "s1",
        [
            ("2026-10-02T14:00:00Z", "expired", CONNECTED),
            ("2026-10-02T15:00:00Z", "expired", SESSION_EXPIRED),
            ("2026-10-02T14:00:00Z", "healed", CONNECTED),
            ("2026-10-02T15:00:00Z", "healed", SESSION_EXPIRED),
            ("2026-10-02T15:00:05Z", "healed", RECONNECTED),
            ("2026-10-02T14:00:00Z", "stdio", CONNECTED),
        ],
    )
    _log(
        cache,
        "-a",
        "c1",
        [("2026-10-02T16:00:00Z", "stdio", STDIO_GONE)],
        "claude-context",
    )
    assert _run(capsys) == {"%1": "DOWN", "%2": "ok", "%3": "ok"}  # precis column
    assert _module().main(["--server", "claude-context"]) == 0
    assert dict(line.split() for line in capsys.readouterr().out.splitlines()) == {
        "%1": "-",
        "%2": "-",
        "%3": "DOWN",
    }


def test_servers_flag_prints_one_column_per_server(
    dirs: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    sessions, cache = dirs
    _session(sessions, 1, "a", "%1")
    _session(sessions, 2, "b", "%2")
    _log(cache, "-a", "p", [("2026-10-02T14:00:00Z", "a", GAVE_UP)])
    _log(
        cache,
        "-a",
        "c",
        [("2026-10-02T14:00:00Z", "b", CONNECTED)],
        "claude-context",
    )
    assert _module().main(["--servers", "precis,claude-context"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "%1 precis=DOWN claude-context=-",
        "%2 precis=- claude-context=ok",
    ]

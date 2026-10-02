"""scripts/fleet wait_mcp: windows start only once the session MCP server answers HTTP.

A session connects to the shared server once, at start, and gives up after
~7 s of refused connects; after a reboot the orchestrator started before the
server came up and never had precis (gr460711, 2026-10-02 11:34Z).
"""

from __future__ import annotations

import http.server
import os
import socket
import subprocess
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

FLEET = Path(__file__).resolve().parents[1] / "scripts" / "fleet"


def _function() -> str:
    lines = FLEET.read_text(encoding="utf-8").splitlines()
    start = lines.index("wait_mcp() {")
    return "\n".join(lines[start : lines.index("}", start) + 1])


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_mcp(
    tmp_path: Path, port: int, limit: int, *, ensure: bool = True
) -> tuple[subprocess.CompletedProcess[str], float]:
    script = tmp_path / ("ensure.sh" if ensure else "absent.sh")
    if ensure:
        script.write_text(
            f"#!/bin/bash\necho called >> {tmp_path / 'calls'}\n", encoding="utf-8"
        )
        script.chmod(0o755)
    env = {
        **os.environ,
        # Never the real ensure script: it would recreate the live server.
        "PRECIS_MCP_ENSURE": str(script),
        "PRECIS_MCP_HTTP_PORT": str(port),
        "PRECIS_FLEET_MCP_WAIT": str(limit),
    }
    start = time.monotonic()
    result = subprocess.run(
        ["bash", "-c", f"set -euo pipefail\n{_function()}\nwait_mcp\n"],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    return result, time.monotonic() - start


def _ensure_calls(tmp_path: Path) -> int:
    calls = tmp_path / "calls"
    return len(calls.read_text(encoding="utf-8").splitlines()) if calls.exists() else 0


@pytest.fixture
def http_port() -> Iterator[int]:
    """Any HTTP answer counts as up — the real server says 401 without a token."""
    server = http.server.HTTPServer(
        ("127.0.0.1", 0), http.server.BaseHTTPRequestHandler
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


def test_returns_once_the_server_answers(tmp_path: Path, http_port: int) -> None:
    result, took = _wait_mcp(tmp_path, http_port, 30)
    assert result.returncode == 0, result.stderr
    assert took < 10
    assert _ensure_calls(tmp_path) == 1
    assert "waiting" not in result.stdout


def test_gives_up_after_the_limit_and_says_to_reconnect(tmp_path: Path) -> None:
    result, took = _wait_mcp(tmp_path, _free_port(), 4)
    assert result.returncode == 0, result.stderr
    assert took >= 4
    assert "waiting for the session MCP server" in result.stdout
    assert "run /mcp in each" in result.stderr


def test_a_listener_that_never_answers_is_not_ready(tmp_path: Path) -> None:
    """The supervisor holds the port during its prepare step: connects succeed
    but no HTTP answer comes, and a session started then would still fail."""
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(16)
        result, took = _wait_mcp(tmp_path, listener.getsockname()[1], 4)
    assert took >= 4
    assert "run /mcp in each" in result.stderr


def test_disabled_or_without_an_ensure_script_it_does_not_wait(tmp_path: Path) -> None:
    dead = _free_port()
    result, took = _wait_mcp(tmp_path, dead, 0)
    assert (result.returncode, _ensure_calls(tmp_path)) == (0, 0)
    assert took < 5
    result, took = _wait_mcp(tmp_path, dead, 30, ensure=False)
    assert result.returncode == 0
    assert took < 5

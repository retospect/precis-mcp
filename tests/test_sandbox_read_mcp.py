"""sandbox_run's ``precis_access:read`` dial (design §"Precis access") —
the per-run, token'd, read-only MCP callback. Pure helpers (DSN
derivation, argv/env/mcp.json shape) are asserted directly; the
impure spawn/reap paths use a REAL stand-in child process (``sleep``,
or a SIGTERM-ignoring one) rather than a fake pid an ``os.kill`` on
would be meaningless — "test with fake PIDs" (spec) means a stand-in
process, not a bogus number.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, cast

import pytest

from precis.workers.executors import _sandbox_read_mcp as read_mcp

# ── pure: DSN derivation ────────────────────────────────────────────


class TestReadOnlyDatabaseUrl:
    def test_none_in_none_out(self) -> None:
        assert read_mcp.read_only_database_url(None) is None

    def test_empty_in_none_out(self) -> None:
        assert read_mcp.read_only_database_url("") is None

    def test_swaps_user_and_strips_password(self) -> None:
        url = read_mcp.read_only_database_url(
            "postgresql://deploy:s3cr3t@caspar:6432/precis_prod"
        )
        assert url == "postgresql://agent_ro@caspar:6432/precis_prod"

    def test_preserves_host_port_db_with_no_password_in_source(self) -> None:
        url = read_mcp.read_only_database_url(
            "postgresql://deploy@localhost:5432/precis_test"
        )
        assert url == "postgresql://agent_ro@localhost:5432/precis_test"

    def test_no_port_in_source_omits_port(self) -> None:
        url = read_mcp.read_only_database_url("postgresql://deploy@dbhost/precis")
        assert url == "postgresql://agent_ro@dbhost/precis"


# ── pure: argv / env / mcp.json shape ───────────────────────────────


class TestBuildServeArgv:
    def test_shape(self) -> None:
        argv = read_mcp.build_serve_argv(host="127.0.0.1", port=54321, token="tok-abc")
        assert argv[0] == sys.executable
        assert argv[1:4] == ["-m", "precis", "serve"]
        assert "--transport" in argv
        assert argv[argv.index("--transport") + 1] == "streamable-http"
        assert argv[argv.index("--host") + 1] == "127.0.0.1"
        assert argv[argv.index("--port") + 1] == "54321"
        assert argv[argv.index("--token") + 1] == "tok-abc"


class TestBuildServeEnv:
    def test_overrides_dsn_and_role_preserves_rest(self) -> None:
        base = {
            "PATH": "/usr/bin",
            "HOME": "/home/precis",
            "PRECIS_ROOT": "/data/precis",
            "PRECIS_DATABASE_URL": "postgresql://deploy@x/y",
        }
        env = read_mcp.build_serve_env(base, ro_dsn="postgresql://agent_ro@x/y")
        assert env["PATH"] == "/usr/bin"
        assert env["HOME"] == "/home/precis"
        assert env["PRECIS_ROOT"] == "/data/precis"
        assert env["PRECIS_DATABASE_URL"] == "postgresql://agent_ro@x/y"
        assert env["PRECIS_MCP_DB_ROLE"] == "agent_ro"
        # base_env is not mutated in place.
        assert base["PRECIS_DATABASE_URL"] == "postgresql://deploy@x/y"

    def test_strips_credential_shaped_vars_from_polluted_parent_env(self) -> None:
        # Finding 2: the daemon's own env can carry a live Claude Max
        # OAuth token (claude_docker._launch_build's
        # os.environ.setdefault("CLAUDE_CODE_OAUTH_TOKEN", ...)) and other
        # secrets — none of it may reach this network-reachable sidecar's
        # child env.
        base = {
            "PATH": "/usr/bin",
            "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-secret",
            "ANTHROPIC_API_KEY": "sk-ant-api-secret",
            "SOME_SERVICE_TOKEN": "tok-secret",
            "GITHUB_API_KEY": "key-secret",
            "DB_SECRET": "shh",
            "ADMIN_PASSWORD": "hunter2",
            "PRECIS_HARMLESS_VAR": "keep-me",
        }
        env = read_mcp.build_serve_env(base, ro_dsn="postgresql://agent_ro@x/y")
        for credential_name in (
            "CLAUDE_CODE_OAUTH_TOKEN",
            "ANTHROPIC_API_KEY",
            "SOME_SERVICE_TOKEN",
            "GITHUB_API_KEY",
            "DB_SECRET",
            "ADMIN_PASSWORD",
        ):
            assert credential_name not in env
        assert env["PATH"] == "/usr/bin"
        assert env["PRECIS_HARMLESS_VAR"] == "keep-me"
        assert env["PRECIS_DATABASE_URL"] == "postgresql://agent_ro@x/y"
        assert env["PRECIS_MCP_DB_ROLE"] == "agent_ro"


class TestMcpJsonPayload:
    def test_shape_has_bearer_header_not_url(self) -> None:
        payload = read_mcp.mcp_json_payload(
            url="http://10.0.2.2:5555/mcp", token="tok-xyz"
        )
        server_cfg = payload["mcpServers"]["precis"]
        assert server_cfg["type"] == "http"
        assert server_cfg["url"] == "http://10.0.2.2:5555/mcp"
        assert server_cfg["headers"]["Authorization"] == "Bearer tok-xyz"
        assert "tok-xyz" not in server_cfg["url"]

    def test_write_mcp_json_lands_on_disk(self, tmp_path: Path) -> None:
        path = read_mcp.write_mcp_json(tmp_path, port=6001, token="tok-1")
        assert path == tmp_path / "mcp.json"
        assert path.is_file()
        import json

        data = json.loads(path.read_text())
        assert data["mcpServers"]["precis"]["url"] == "http://10.0.2.2:6001/mcp"


# ── impure: spawn (fake DSN/port, no real subprocess) ───────────────


class _FakeStore:
    def __init__(self, dsn: str | None) -> None:
        self.dsn = dsn


class TestSpawnReadMcp:
    def test_raises_when_no_base_dsn(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="no base DSN"):
            read_mcp.spawn_read_mcp(cast(Any, _FakeStore(None)), work_dir=tmp_path)

    def test_spawns_writes_mcp_json_and_returns_handle(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Stub the actual process launch — this test is about the
        # orchestration (DSN check, port pick, mcp.json write, handle
        # shape), not actually spawning `python -m precis serve`.
        captured: dict[str, object] = {}

        class _FakeProc:
            pid = 424242

        def fake_popen(argv: list[str], *, env: dict[str, str], **kw: object) -> object:
            captured["argv"] = argv
            captured["env"] = env
            return _FakeProc()

        monkeypatch.setattr(read_mcp.subprocess, "Popen", fake_popen)

        handle = read_mcp.spawn_read_mcp(
            cast(Any, _FakeStore("postgresql://deploy@dbhost:5432/precis")),
            work_dir=tmp_path,
            port_picker=lambda host: 7001,
        )
        assert handle.pid == 424242
        assert handle.port == 7001
        assert handle.token  # a real random token, non-empty
        assert (tmp_path / "mcp.json").is_file()
        env = captured["env"]
        assert isinstance(env, dict)
        assert env["PRECIS_DATABASE_URL"] == "postgresql://agent_ro@dbhost:5432/precis"


# ── pid-recycle guard: identity check before signaling ──────────────


class TestReadMcpIdentityOk:
    def test_true_when_command_line_names_precis_and_serve(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake_run(argv: list[str], **kw: object) -> Any:
            assert argv[:2] == ["ps", "-p"]
            return subprocess.CompletedProcess(
                argv, 0, stdout="/usr/bin/python3 -m precis serve --transport ...\n"
            )

        monkeypatch.setattr(read_mcp.subprocess, "run", fake_run)
        assert read_mcp._read_mcp_identity_ok(4242) is True

    def test_false_when_command_line_is_unrelated(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake_run(argv: list[str], **kw: object) -> Any:
            return subprocess.CompletedProcess(argv, 0, stdout="sleep 300\n")

        monkeypatch.setattr(read_mcp.subprocess, "run", fake_run)
        assert read_mcp._read_mcp_identity_ok(4242) is False

    def test_false_when_ps_exits_nonzero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_run(argv: list[str], **kw: object) -> Any:
            return subprocess.CompletedProcess(argv, 1, stdout="")

        monkeypatch.setattr(read_mcp.subprocess, "run", fake_run)
        assert read_mcp._read_mcp_identity_ok(4242) is False

    def test_false_when_ps_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_run(argv: list[str], **kw: object) -> Any:
            raise OSError("ps not found")

        monkeypatch.setattr(read_mcp.subprocess, "run", fake_run)
        assert read_mcp._read_mcp_identity_ok(4242) is False


class TestReapReadMcpSkipsPidRecycle:
    def test_skips_signaling_when_pid_is_not_ours(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Finding 3: a bare pid persisted hours earlier could have been
        # recycled by the OS onto an unrelated, innocent process — reap
        # must NOT signal it. Fake identity check False + a kill() spy
        # that fails the test if it's ever called.
        monkeypatch.setattr(read_mcp, "_read_mcp_identity_ok", lambda pid: False)

        def fail_kill(pid: int, sig: int) -> None:
            raise AssertionError("os.kill must not be called for a non-owned pid")

        monkeypatch.setattr(read_mcp.os, "kill", fail_kill)
        read_mcp.reap_read_mcp(999999, grace_s=0.1)  # must not raise

    def test_signals_when_identity_check_passes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(read_mcp, "_read_mcp_identity_ok", lambda pid: True)
        calls: list[tuple[int, int]] = []

        def fake_kill(pid: int, sig: int) -> None:
            calls.append((pid, sig))
            if sig == signal.SIGTERM:
                return
            raise ProcessLookupError

        monkeypatch.setattr(read_mcp.os, "kill", fake_kill)
        read_mcp.reap_read_mcp(4242, grace_s=0.1)
        assert calls[0] == (4242, signal.SIGTERM)


# ── impure: reap (real stand-in child processes) ────────────────────


def _spawn_sleeper() -> subprocess.Popen[bytes]:
    return subprocess.Popen(["sleep", "300"])


def _spawn_sigterm_ignorer() -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import signal, time; "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "time.sleep(300)",
        ]
    )


def _is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class TestReapReadMcp:
    def test_sigterm_reaps_a_cooperative_child(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The stand-in child is a bare `sleep`, not a real `precis serve`
        # — bypass the Finding-3 identity check (asserted separately in
        # TestReadMcpIdentityOk / TestReapReadMcpSkipsPidRecycle above) so
        # this test can focus on the SIGTERM/SIGKILL escalation shape.
        monkeypatch.setattr(read_mcp, "_read_mcp_identity_ok", lambda pid: True)
        proc = _spawn_sleeper()
        try:
            assert _is_alive(proc.pid)
            read_mcp.reap_read_mcp(proc.pid, grace_s=2.0)
            proc.wait(timeout=2)
            assert not _is_alive(proc.pid)
        finally:
            if _is_alive(proc.pid):  # pragma: no cover - safety net
                proc.kill()
                proc.wait(timeout=2)

    def test_escalates_to_sigkill_for_a_sigterm_ignoring_child(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(read_mcp, "_read_mcp_identity_ok", lambda pid: True)
        proc = _spawn_sigterm_ignorer()
        try:
            time.sleep(0.2)  # let the signal handler install
            assert _is_alive(proc.pid)
            read_mcp.reap_read_mcp(proc.pid, grace_s=0.5)
            proc.wait(timeout=2)
            assert not _is_alive(proc.pid)
        finally:
            if _is_alive(proc.pid):  # pragma: no cover - safety net
                os.kill(proc.pid, signal.SIGKILL)
                proc.wait(timeout=2)

    def test_already_dead_pid_is_a_silent_no_op(self) -> None:
        proc = _spawn_sleeper()
        proc.kill()
        proc.wait(timeout=2)
        assert not _is_alive(proc.pid)
        # A dead pid fails the real identity check too (ps -p exits
        # nonzero) — reap_read_mcp must still not raise.
        read_mcp.reap_read_mcp(proc.pid, grace_s=0.2)  # must not raise

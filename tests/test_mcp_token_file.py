"""The precis MCP bearer token lives in mode-600 files, not in
``~/.claude.json``'s inline ``Authorization`` header (gr515184).

``claude mcp get precis`` used to print the token in plaintext — an easy
leak into scrollback, transcripts and logs. The client entry now carries
the env reference ``${PRECIS_MCP_TOKEN}``, which Claude Code expands at
connect time; ``scripts/mcp-token-env`` fills that variable from the
client token file at session launch, and ``scripts/mcp-token-check`` is
the ansible task's assert step (stat owner and mode on both files, then
one authenticated MCP call).

What is pinned:

* the ensure script's ``--config`` prints the ``${PRECIS_MCP_TOKEN}``
  reference and never the token value, and still mints the server
  secret mode 600;
* ``mcp-token-env`` exports the file's value into the child's
  environment without printing it, falls back to the shared server's
  ``~/.secrets/pw`` file, and warns-but-execs when nothing is readable;
* ``wt_mcp._session_mcp_env`` fills the ``claude`` child's env the same
  way (an existing ``PRECIS_MCP_TOKEN`` wins);
* ``mcp-token-check`` passes a correct rig and names the first failure —
  wrong mode, wrong owner, missing file, parent dir not user-owned, or
  a 401 from the authenticated call;
* every ``claude`` launch site in ``scripts/fleet``/``claude-local``
  goes through ``mcp-token-env``.

All tokens are fakes in tmp dirs; the real ``~/.secrets``, user config
and session server are never touched.
"""

from __future__ import annotations

import http.server
import json
import os
import stat
import subprocess
import sys
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX-only: bash scripts and mode bits"
)

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import wt_mcp

ENSURE = REPO / "deploy" / "mcp-http" / "precis-mcp-http-ensure.sh"
TOKEN_ENV = REPO / "scripts" / "mcp-token-env"
TOKEN_CHECK = REPO / "scripts" / "mcp-token-check"
FLEET = REPO / "scripts" / "fleet"
CLAUDE_LOCAL = REPO / "scripts" / "claude-local"

FAKE_TOKEN = "fake-token-0123456789abcdef0123456789abcdef"


def _run(
    argv: list[str], env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        env={**os.environ, **(env or {})},
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )


# --- the ensure script's --config -------------------------------------------


def test_ensure_config_emits_the_env_reference_not_the_token(
    tmp_path: Path,
) -> None:
    """``--config`` output is fed to ``claude mcp add-json`` and echoed by
    ``claude mcp get``: the bearer header must be the ``${PRECIS_MCP_TOKEN}``
    reference, never the value it minted into ~/.secrets/pw."""
    res = subprocess.run(
        ["bash", str(ENSURE), "--config"],
        env={"HOME": str(tmp_path), "PATH": os.environ["PATH"]},
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )
    assert res.returncode == 0, res.stderr
    entry = json.loads(res.stdout.strip().splitlines()[-1])
    assert entry["headers"]["Authorization"] == "Bearer ${PRECIS_MCP_TOKEN}"
    minted = tmp_path / ".secrets" / "pw" / "PRECIS_MCP_TOKEN"
    assert minted.is_file()
    assert stat.S_IMODE(minted.stat().st_mode) == 0o600
    value = minted.read_text(encoding="utf-8").strip()
    assert value and value not in res.stdout + res.stderr


# --- scripts/mcp-token-env ---------------------------------------------------


def _env_seen(argv_env: dict[str, str], out: Path) -> subprocess.CompletedProcess[str]:
    return _run(
        [
            "bash",
            str(TOKEN_ENV),
            "bash",
            "-c",
            f'printf %s "$PRECIS_MCP_TOKEN" > "{out}"',
        ],
        argv_env,
    )


def test_token_env_exports_the_file_value(tmp_path: Path) -> None:
    file = tmp_path / "token"
    file.write_text(f"{FAKE_TOKEN}\n", encoding="utf-8")
    file.chmod(0o600)
    out = tmp_path / "seen"
    res = _env_seen({"PRECIS_MCP_TOKEN_FILE": str(file), "HOME": str(tmp_path)}, out)
    assert res.returncode == 0, res.stderr
    assert out.read_text(encoding="utf-8") == FAKE_TOKEN
    assert FAKE_TOKEN not in res.stdout + res.stderr


def test_token_env_falls_back_to_the_secrets_dir(tmp_path: Path) -> None:
    """Until ansible lands ~/.config/precis/token the shared server's own
    file carries the same value — sessions must not go dark in between."""
    secrets = tmp_path / ".secrets" / "pw"
    secrets.mkdir(parents=True)
    (secrets / "PRECIS_MCP_TOKEN").write_text(FAKE_TOKEN, encoding="utf-8")
    (secrets / "PRECIS_MCP_TOKEN").chmod(0o600)
    out = tmp_path / "seen"
    res = _env_seen({"HOME": str(tmp_path)}, out)
    assert res.returncode == 0, res.stderr
    assert out.read_text(encoding="utf-8") == FAKE_TOKEN


def test_token_env_refuses_a_too_open_file(tmp_path: Path) -> None:
    """Owner/mode gate (gr515184 review): a group/world-readable token
    file is refused before any read — path + chmod hint on stderr, never
    the content, and the child still execs without the var."""
    file = tmp_path / "token"
    file.write_text(FAKE_TOKEN, encoding="utf-8")
    file.chmod(0o644)
    out = tmp_path / "seen"
    res = _env_seen({"PRECIS_MCP_TOKEN_FILE": str(file), "HOME": str(tmp_path)}, out)
    assert res.returncode == 0
    assert "refusing" in res.stderr and f"chmod 600 {file}" in res.stderr
    assert FAKE_TOKEN not in res.stdout + res.stderr
    assert out.read_text(encoding="utf-8") == ""


def test_token_env_follows_a_symlink_to_a_0600_file(tmp_path: Path) -> None:
    """stat without -L checks the link's own mode (0777) and refuses it;
    wt_mcp follows links, so the bash gate must too (gr515184)."""
    target = tmp_path / "real-token"
    target.write_text(FAKE_TOKEN, encoding="utf-8")
    target.chmod(0o600)
    link = tmp_path / "token"
    link.symlink_to(target)
    out = tmp_path / "seen"
    res = _env_seen({"PRECIS_MCP_TOKEN_FILE": str(link), "HOME": str(tmp_path)}, out)
    assert res.returncode == 0, res.stderr
    assert "refusing" not in res.stderr
    assert out.read_text(encoding="utf-8") == FAKE_TOKEN
    assert FAKE_TOKEN not in res.stdout + res.stderr


def test_token_env_refuses_a_symlink_to_a_0644_file(tmp_path: Path) -> None:
    target = tmp_path / "real-token"
    target.write_text(FAKE_TOKEN, encoding="utf-8")
    target.chmod(0o644)
    link = tmp_path / "token"
    link.symlink_to(target)
    out = tmp_path / "seen"
    res = _env_seen({"PRECIS_MCP_TOKEN_FILE": str(link), "HOME": str(tmp_path)}, out)
    assert res.returncode == 0
    assert "refusing" in res.stderr and f"chmod 600 {link}" in res.stderr
    assert FAKE_TOKEN not in res.stdout + res.stderr
    assert out.read_text(encoding="utf-8") == ""


def test_token_env_refuses_a_foreign_owned_file(tmp_path: Path) -> None:
    """A file owned by another uid is refused the same way — fake stat/id
    on PATH stand in for chown (tests can't chown)."""
    file = tmp_path / "token"
    file.write_text(FAKE_TOKEN, encoding="utf-8")
    file.chmod(0o600)
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    fake_stat = fakebin / "stat"
    fake_stat.write_text("#!/bin/sh\nprintf '99999 600\\n'\n", encoding="utf-8")
    fake_stat.chmod(0o755)
    out = tmp_path / "seen"
    res = _env_seen(
        {
            "PRECIS_MCP_TOKEN_FILE": str(file),
            "HOME": str(tmp_path),
            "PATH": f"{fakebin}:{os.environ['PATH']}",
        },
        out,
    )
    assert res.returncode == 0
    assert "refusing" in res.stderr
    assert FAKE_TOKEN not in res.stdout + res.stderr
    assert out.read_text(encoding="utf-8") == ""


def test_token_env_xtrace_cannot_echo_the_assignment(tmp_path: Path) -> None:
    """`bash -x` / SHELLOPTS=xtrace would echo `+ PRECIS_MCP_TOKEN=<value>`
    — the `set +x` guard is the first line after the shebang."""
    file = tmp_path / "token"
    file.write_text(FAKE_TOKEN, encoding="utf-8")
    file.chmod(0o600)
    out = tmp_path / "seen"
    res = _run(
        [
            "bash",
            "-x",
            str(TOKEN_ENV),
            "bash",
            "-c",
            f'printf %s "$PRECIS_MCP_TOKEN" > "{out}"',
        ],
        {"PRECIS_MCP_TOKEN_FILE": str(file), "HOME": str(tmp_path)},
    )
    assert res.returncode == 0, res.stderr
    assert out.read_text(encoding="utf-8") == FAKE_TOKEN
    assert FAKE_TOKEN not in res.stdout + res.stderr


def test_token_env_warns_but_still_execs_without_a_file(tmp_path: Path) -> None:
    """A session without precis auth beats no session — the entry
    preflight names the restart runbook."""
    res = _run(
        ["bash", str(TOKEN_ENV), "bash", "-c", "echo still-ran"],
        {"HOME": str(tmp_path)},
    )
    assert res.returncode == 0
    assert "still-ran" in res.stdout
    assert "no readable client token file" in res.stderr


def test_token_env_usage_error(tmp_path: Path) -> None:
    res = _run(["bash", str(TOKEN_ENV)], {"HOME": str(tmp_path)})
    assert res.returncode == 2
    assert "usage" in res.stderr


# --- wt_mcp._session_mcp_env (the `wt-mcp claude` child) ---------------------


def test_wt_session_env_reads_the_token_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    file = tmp_path / "token"
    file.write_text(f"{FAKE_TOKEN}\n", encoding="utf-8")
    file.chmod(0o600)
    monkeypatch.delenv("PRECIS_MCP_TOKEN", raising=False)
    monkeypatch.setenv("PRECIS_MCP_TOKEN_FILE", str(file))
    monkeypatch.setenv("HOME", str(tmp_path))
    env = wt_mcp._session_mcp_env()
    assert env["PRECIS_MCP_TOKEN"] == FAKE_TOKEN


def test_wt_session_env_refuses_a_too_open_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    file = tmp_path / "token"
    file.write_text(FAKE_TOKEN, encoding="utf-8")
    file.chmod(0o644)
    monkeypatch.delenv("PRECIS_MCP_TOKEN", raising=False)
    monkeypatch.setenv("PRECIS_MCP_TOKEN_FILE", str(file))
    monkeypatch.setenv("HOME", str(tmp_path))
    env = wt_mcp._session_mcp_env()
    assert "PRECIS_MCP_TOKEN" not in env
    err = capsys.readouterr().err
    assert "refusing" in err and f"chmod 600 {file}" in err
    assert FAKE_TOKEN not in err


def test_wt_session_env_refuses_a_foreign_owned_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    file = tmp_path / "token"
    file.write_text(FAKE_TOKEN, encoding="utf-8")
    file.chmod(0o600)
    monkeypatch.delenv("PRECIS_MCP_TOKEN", raising=False)
    monkeypatch.setenv("PRECIS_MCP_TOKEN_FILE", str(file))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(os, "getuid", lambda: file.stat().st_uid + 1)
    env = wt_mcp._session_mcp_env()
    assert "PRECIS_MCP_TOKEN" not in env
    err = capsys.readouterr().err
    assert "refusing" in err and FAKE_TOKEN not in err


def test_wt_session_env_keeps_an_existing_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRECIS_MCP_TOKEN", "already-set")
    env = wt_mcp._session_mcp_env()
    assert env["PRECIS_MCP_TOKEN"] == "already-set"


def test_wt_session_env_warns_without_any_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("PRECIS_MCP_TOKEN", raising=False)
    monkeypatch.delenv("PRECIS_MCP_TOKEN_FILE", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    env = wt_mcp._session_mcp_env()
    assert "PRECIS_MCP_TOKEN" not in env
    assert "no client token file" in capsys.readouterr().err


# --- scripts/mcp-token-check (the ansible assert) ----------------------------


class _McpHandler(http.server.BaseHTTPRequestHandler):
    """POST /mcp: 200 only for the rig's bearer token, else 401."""

    good = ""

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        if self.headers.get("Authorization") == f"Bearer {self.good}":
            body = b'{"jsonrpc":"2.0","id":1,"result":{}}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
        else:
            body = b"unauthorized"
            self.send_response(401)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: object) -> None:
        pass


@dataclass
class Rig:
    client: Path
    server: Path
    env: dict[str, str]


@pytest.fixture
def check_rig(tmp_path: Path) -> Iterator[Rig]:
    """Both token files in tmp, a fake session server on an ephemeral port."""
    client_dir = tmp_path / ".config" / "precis"
    client_dir.mkdir(parents=True)
    client = client_dir / "token"
    client.write_text(f"{FAKE_TOKEN}\n", encoding="utf-8")
    client.chmod(0o600)
    server_secrets = tmp_path / "state" / "secrets"
    server_secrets.mkdir(parents=True)
    server = server_secrets / "PRECIS_MCP_TOKEN"
    server.write_text(FAKE_TOKEN, encoding="utf-8")
    server.chmod(0o600)

    _McpHandler.good = FAKE_TOKEN
    httpd = http.server.HTTPServer(("127.0.0.1", 0), _McpHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    yield Rig(
        client=client,
        server=server,
        env={
            "PRECIS_MCP_TOKEN_FILE": str(client),
            "PRECIS_MCP_HTTP_STATE": str(tmp_path / "state"),
            "PRECIS_MCP_HTTP_PORT": str(httpd.server_port),
            "PRECIS_MCP_CONTAINER_UID": str(os.getuid()),
        },
    )
    httpd.shutdown()


def _check(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return _run([sys.executable, str(TOKEN_CHECK)], env)


def test_check_passes_a_correct_rig(check_rig: Rig) -> None:
    res = _check(check_rig.env)
    assert res.returncode == 0, res.stderr
    assert "all checks passed" in res.stdout
    assert FAKE_TOKEN not in res.stdout + res.stderr


def test_check_names_a_wrong_mode(check_rig: Rig) -> None:
    check_rig.client.chmod(0o644)
    res = _check(check_rig.env)
    assert res.returncode == 1
    assert "mode 0644" in res.stderr and "group/other" in res.stderr
    assert FAKE_TOKEN not in res.stdout + res.stderr


def test_check_accepts_a_stricter_mode(check_rig: Rig) -> None:
    """0600 or stricter is fine — a read-only owner file must not fail."""
    check_rig.client.chmod(0o400)
    check_rig.server.chmod(0o400)
    res = _check(check_rig.env)
    assert res.returncode == 0, res.stderr


def test_check_assumes_invoking_uid_without_a_backend(check_rig: Rig) -> None:
    """No live backend and no PRECIS_MCP_CONTAINER_UID: the check assumes
    the invoking uid (the dev image is built with the host uid), not a
    blanket failure. PATH without docker skips the exec probe."""
    env = {k: v for k, v in check_rig.env.items() if k != "PRECIS_MCP_CONTAINER_UID"}
    env["PATH"] = str(Path(check_rig.env["PRECIS_MCP_TOKEN_FILE"]).parent)
    res = _check(env)
    assert res.returncode == 0, res.stderr
    assert "assuming invoking uid" in res.stdout


def test_check_names_a_missing_server_file(check_rig: Rig) -> None:
    check_rig.server.unlink()
    res = _check(check_rig.env)
    assert res.returncode == 1
    assert "server token file" in res.stderr


def test_check_names_a_uid_mismatch(check_rig: Rig) -> None:
    """A 0600 secrets file owned by the wrong uid locks the container out."""
    res = _check({**check_rig.env, "PRECIS_MCP_CONTAINER_UID": str(os.getuid() + 1)})
    assert res.returncode == 1
    assert "owned by uid" in res.stderr and "server token file" in res.stderr


def test_check_names_a_401(check_rig: Rig) -> None:
    """Client file and mounted secret disagree: the authed call answers 401."""
    _McpHandler.good = "a-different-token"
    res = _check(check_rig.env)
    assert res.returncode == 1
    assert "401" in res.stderr


def test_check_names_a_down_server(check_rig: Rig) -> None:
    res = _check({**check_rig.env, "PRECIS_MCP_HTTP_PORT": "1"})
    assert res.returncode == 1
    assert "no answer" in res.stderr


# --- every claude launch site routes through mcp-token-env -------------------


def test_fleet_launches_claude_through_token_env() -> None:
    src = FLEET.read_text(encoding="utf-8")
    assert src.count("mcp-token-env' claude") == 3
    assert '"claude ' not in src


def test_claude_local_execs_through_token_env() -> None:
    src = CLAUDE_LOCAL.read_text(encoding="utf-8")
    assert 'exec "$(dirname "$0")/mcp-token-env" claude' in src


def test_wt_mcp_claude_child_gets_the_env() -> None:
    src = (REPO / "scripts" / "wt_mcp.py").read_text(encoding="utf-8")
    assert "env=_session_mcp_env()" in src

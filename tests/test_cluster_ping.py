"""scripts/cluster-ping — one line per inventory host, nonzero if one is down.

Runs the real script against a fixture inventory (placeholder names,
RFC-5737 addresses) with a fake ``ssh`` and ``ping`` first on PATH.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX-only: execs a bash script with fake bins"
)

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "cluster-ping"

_INVENTORY = """\
all:
  vars:
    postgres_host: node-data
  children:
    macos:
      hosts:
        node-gateway:
          ansible_host: 203.0.113.10
          lan_ip: 192.0.2.10
        node-data:
          ansible_host: "203.0.113.12"
    linux:
      hosts:
        node-gpu:
          ansible_host: 203.0.113.20
        node-flaky:
          ansible_host: 203.0.113.21
        node-auth:
          ansible_host: 203.0.113.22
    llamacpp_hosts:
      hosts:
        node-gateway:
        node-gpu:
"""

# ssh: log argv; node-gpu always times out (255), node-flaky times out on
# its first try only, node-auth fails auth (255, no "timed out").
# ping: fail for .20.
_FAKE_SSH = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$FAKE_LOG"
for a in "$@"; do last2="$last1"; last1="$a"; done
if [[ "$last2" == node-gpu || "$last2" == 203.0.113.20 ]]; then
    echo "ssh: connect to host node-gpu port 22: Operation timed out" >&2
    exit 255
fi
if [[ "$last2" == node-flaky && ! -e "$FAKE_LOG.flaky" ]]; then
    : > "$FAKE_LOG.flaky"
    echo "ssh: connect to host node-flaky port 22: Operation timed out" >&2
    exit 255
fi
if [[ "$last2" == node-auth ]]; then
    echo "deploy@node-auth: Permission denied (publickey)." >&2
    exit 255
fi
echo " 12:00  up 3 days,  2:01, 1 user, load averages: 1.0 1.1 1.2"
"""
_FAKE_PING = """#!/usr/bin/env bash
printf 'ping %s\\n' "$*" >> "$FAKE_LOG"
[[ "${@: -1}" == 203.0.113.20 ]] && exit 2
exit 0
"""


def _run(tmp_path: Path, *args: str) -> tuple[subprocess.CompletedProcess[str], str]:
    inv = tmp_path / "hosts.yml"
    inv.write_text(_INVENTORY, encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for name, body in (("ssh", _FAKE_SSH), ("ping", _FAKE_PING)):
        exe = bin_dir / name
        exe.write_text(body, encoding="utf-8")
        exe.chmod(0o755)
    log = tmp_path / "calls.log"
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "CLUSTER_INVENTORY": str(inv),
        "FAKE_LOG": str(log),
    }
    env.pop("CLUSTER_PING_SSH_OPTS", None)
    proc = subprocess.run(
        [str(_SCRIPT), *args],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    return proc, log.read_text(encoding="utf-8") if log.exists() else ""


def test_one_line_per_host_and_nonzero_when_one_is_down(tmp_path: Path) -> None:
    proc, _ = _run(tmp_path, "node-gateway", "node-data", "node-gpu")
    lines = proc.stdout.splitlines()
    assert [ln.split()[0] for ln in lines] == ["node-gateway", "node-data", "node-gpu"]
    assert "ping ok" in lines[0] and "ssh ok" in lines[0] and "up 3 days" in lines[0]
    assert "ping fail" in lines[2] and "ssh fail" in lines[2]
    assert "exit 255" in lines[2] and "timed out" in lines[2]
    assert proc.returncode == 1


def test_all_up_exits_zero(tmp_path: Path) -> None:
    proc, _ = _run(tmp_path, "node-gateway", "node-data")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert len(proc.stdout.splitlines()) == 2


def test_ssh_is_bounded_by_options_not_timeout(tmp_path: Path) -> None:
    _, log = _run(tmp_path, "node-data")
    ssh_line = next(ln for ln in log.splitlines() if not ln.startswith("ping "))
    for opt in (
        "BatchMode=yes",
        "ConnectTimeout=8",
        "ServerAliveInterval=5",
        "ServerAliveCountMax=2",
    ):
        assert opt in ssh_line
    assert ssh_line.endswith("node-data uptime")
    assert "ping -c 1" in log and "203.0.113.12" in log


def test_addr_flag_and_unknown_host(tmp_path: Path) -> None:
    _, log = _run(tmp_path, "--addr", "node-data")
    assert "203.0.113.12 uptime" in log
    proc, _ = _run(tmp_path, "node-nope")
    assert proc.returncode == 2
    assert "not" in proc.stderr or "none" in proc.stderr


def _ssh_tries(log: str, host: str) -> int:
    return sum(
        1
        for ln in log.splitlines()
        if not ln.startswith("ping ") and ln.endswith(f"{host} uptime")
    )


def test_connect_timeout_then_ok_is_retried_and_flagged(tmp_path: Path) -> None:
    proc, log = _run(tmp_path, "node-flaky")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "ssh ok (retry)" in proc.stdout and "up 3 days" in proc.stdout
    assert _ssh_tries(log, "node-flaky") == 2


def test_connect_timeout_twice_fails_after_one_retry(tmp_path: Path) -> None:
    proc, log = _run(tmp_path, "node-gpu")
    assert proc.returncode == 1
    assert "ssh fail" in proc.stdout and "timed out" in proc.stdout
    assert _ssh_tries(log, "node-gpu") == 2


def test_auth_failure_is_not_retried(tmp_path: Path) -> None:
    proc, log = _run(tmp_path, "node-auth")
    assert proc.returncode == 1
    assert "ssh fail" in proc.stdout and "Permission denied" in proc.stdout
    assert _ssh_tries(log, "node-auth") == 1

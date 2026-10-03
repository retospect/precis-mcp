"""scripts/test tears a subagent worktree's compose project down at exit.

Every compose project holds one docker network address pool for as long as its
``precis-test-db`` exists; idle subagent dbs exhausted the pool fleet-wide on
2026-10-03. ``scripts/test`` therefore ``compose down -v``s the project at exit
when the worktree basename is ``agent-*`` -- unless ``PRECIS_TEST_KEEP_DB`` is
set, ``--fast`` skipped the db, or another container of the project is running.

These tests run the REAL ``scripts/test`` (staged byte-for-byte, with its
sourced libs, into a throwaway git repo named like the case needs) against a
synthetic ``docker`` on PATH that logs every ``compose`` verb. The real daemon
is never contacted.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="the shipped bash script + fake docker are POSIX-only",
)

REPO = Path(__file__).resolve().parent.parent
_LIBS = ("compose-project.sh", "gate-slot.sh", "lock-holder.sh")

_FAKE_DOCKER = r"""#!/usr/bin/env bash
# Synthetic docker -- logs compose verbs; never touches a daemon.
#   FAKE_DOCKER_LOG       appended: up|<proj> / run|<proj> / down|<flags>|<proj>
#   FAKE_PS_SERVICES      newline-separated service names `docker ps` reports
#   FAKE_RUN_RC           exit status of `compose run` (default 0)
#   FAKE_DOWN_RC          exit status of `compose down` (default 0)
set -u
args=("$@")
case "${args[0]:-}" in
  ps)
    printf '%s\n' "${FAKE_PS_SERVICES:-}"
    exit 0
    ;;
  compose)
    proj=""
    verb=""
    for i in "${!args[@]}"; do
        a="${args[$i]}"
        [ "$a" = "-p" ] && proj="${args[$((i+1))]}"
        if [ -z "$verb" ]; then
            case "$a" in up|run|down) verb="$a" ;; esac
        fi
    done
    case "$verb" in
      up)   echo "up|$proj" >> "$FAKE_DOCKER_LOG"; exit 0 ;;
      run)  echo "run|$proj" >> "$FAKE_DOCKER_LOG"; exit "${FAKE_RUN_RC:-0}" ;;
      down)
        flags=""
        for a in "${args[@]}"; do case "$a" in -v) flags="-v" ;; esac; done
        echo "down|$flags|$proj" >> "$FAKE_DOCKER_LOG"
        exit "${FAKE_DOWN_RC:-0}"
        ;;
    esac
    exit 0
    ;;
esac
exit 0
"""


def _stage(tmp_path: Path, name: str) -> Path:
    """A throwaway repo directory called ``name`` holding the real script."""
    tree = tmp_path / name
    (tree / "scripts" / "lib").mkdir(parents=True)
    (tree / "docker" / "dev").mkdir(parents=True)
    shutil.copy2(REPO / "scripts" / "test", tree / "scripts" / "test")
    for lib in _LIBS:
        shutil.copy2(REPO / "scripts" / "lib" / lib, tree / "scripts" / "lib" / lib)
    (tree / "docker" / "dev" / "compose.yaml").write_text(
        "services: {}\n", encoding="utf-8"
    )
    subprocess.run(["git", "init", "-q", str(tree)], check=True)
    return tree


def _run(
    tmp_path: Path,
    name: str,
    *,
    args: tuple[str, ...] = ("-n0", "tests/x.py"),
    ps_services: str = "precis-test-db",
    run_rc: int = 0,
    down_rc: int = 0,
    keep_db: bool = False,
) -> tuple[int, list[str]]:
    tree = _stage(tmp_path, name)
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir(exist_ok=True)
    docker = fakebin / "docker"
    docker.write_text(_FAKE_DOCKER, encoding="utf-8")
    docker.chmod(0o755)
    log = tmp_path / "docker.log"
    log.write_text("", encoding="utf-8")
    env = {
        **os.environ,
        "PATH": f"{fakebin}{os.pathsep}{os.environ['PATH']}",
        "FAKE_DOCKER_LOG": str(log),
        "FAKE_PS_SERVICES": ps_services,
        "FAKE_RUN_RC": str(run_rc),
        "FAKE_DOWN_RC": str(down_rc),
        "TMPDIR": str(tmp_path),
    }
    env.pop("PRECIS_TEST_KEEP_DB", None)
    env.pop("PRECIS_COMPOSE", None)
    if keep_db:
        env["PRECIS_TEST_KEEP_DB"] = "1"
    proc = subprocess.run(
        ["bash", str(tree / "scripts" / "test"), *args],
        cwd=tree,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    return proc.returncode, log.read_text(encoding="utf-8").splitlines()


def test_agent_tree_with_only_db_running_is_torn_down(tmp_path: Path) -> None:
    rc, log = _run(tmp_path, "agent-abc123")
    assert rc == 0
    assert log == [
        "up|precis-test-agent-abc123",
        "run|precis-test-agent-abc123",
        "down|-v|precis-test-agent-abc123",
    ]


def test_agent_tree_with_another_service_running_is_left_up(tmp_path: Path) -> None:
    rc, log = _run(tmp_path, "agent-abc123", ps_services="precis-test-db\nprecis-dev")
    assert rc == 0
    assert not [line for line in log if line.startswith("down|")]


def test_thread_tree_is_left_up(tmp_path: Path) -> None:
    rc, log = _run(tmp_path, "ship-gate-ci")
    assert rc == 0
    assert [line.split("|")[0] for line in log] == ["up", "run"]


def test_keep_db_env_skips_teardown(tmp_path: Path) -> None:
    rc, log = _run(tmp_path, "agent-abc123", keep_db=True)
    assert rc == 0
    assert not [line for line in log if line.startswith("down|")]


def test_fast_mode_skips_teardown(tmp_path: Path) -> None:
    rc, log = _run(tmp_path, "agent-abc123", args=("--fast", "-n0", "tests/x.py"))
    assert rc == 0
    assert [line.split("|")[0] for line in log] == ["run"]


def test_failed_down_does_not_change_exit_status(tmp_path: Path) -> None:
    rc, log = _run(tmp_path, "agent-abc123", down_rc=1)
    assert rc == 0
    assert "down|-v|precis-test-agent-abc123" in log


def test_test_failure_exit_status_survives_teardown(tmp_path: Path) -> None:
    rc, log = _run(tmp_path, "agent-abc123", run_rc=3, down_rc=1)
    assert rc == 3
    assert "down|-v|precis-test-agent-abc123" in log

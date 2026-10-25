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
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="the shipped bash script + fake docker are POSIX-only",
)

REPO = Path(__file__).resolve().parent.parent
_LIBS = ("compose-project.sh", "docker-disk.sh", "gate-slot.sh", "lock-holder.sh")

_FAKE_DOCKER = r"""#!/usr/bin/env bash
# Synthetic docker -- logs compose verbs; never touches a daemon.
#   FAKE_DOCKER_LOG       appended: up|<proj> / run|<proj> / down|<flags>|<proj>
#   FAKE_PS_SERVICES      newline-separated service names `docker ps` reports
#   FAKE_RUN_RC           exit status of `compose run` (default 0)
#   FAKE_DOWN_RC          exit status of `compose down` (default 0)
# A plain `docker run ... df` is the free-disk preflight probe: it answers with
# 100 GB free so the preflight passes and never reaches the compose log.
set -u
args=("$@")
case "${args[0]:-}" in
  run)
    printf 'Filesystem 1024-blocks Used Available Capacity Mounted on\n'
    printf 'overlay 209715200 1048576 104857600 1%% /\n'
    exit 0
    ;;
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
    # scripts/test refuses a missing test path before any docker call
    # (gr514948) — the staged tree needs the default arg's file to exist.
    (tree / "tests").mkdir(exist_ok=True)
    (tree / "tests" / "x.py").write_text("def test_x():\n    pass\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(tree)], check=True)
    return tree


def _fake_docker_env(
    tmp_path: Path,
    *,
    ps_services: str,
    run_rc: int,
    down_rc: int,
    keep_db: bool,
    extra_env: dict[str, str] | None,
) -> tuple[dict[str, str], Path]:
    """PATH with the synthetic docker first + the env it logs through."""
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
    env.pop("PRECIS_GATE_SLOTS", None)
    if keep_db:
        env["PRECIS_TEST_KEEP_DB"] = "1"
    env.update(extra_env or {})
    return env, log


def _this_host() -> str:
    return (
        subprocess.run(
            ["hostname", "-s"], capture_output=True, text=True, encoding="utf-8"
        ).stdout.strip()
        or "unknown"
    )


def _hold_gate_slot(tree: Path, index: int, pid: int) -> None:
    """Occupy a gate slot in the staged repo as a live same-host holder."""
    d = tree / ".git" / f"precis-gate-slot-{index}.lock.d"
    d.mkdir()
    (d / "holder").write_text(
        f"{tree} pid={pid} host={_this_host()}\n", encoding="utf-8"
    )


def _run(
    tmp_path: Path,
    name: str,
    *,
    args: tuple[str, ...] = ("-n0", "tests/x.py"),
    ps_services: str = "precis-test-db",
    run_rc: int = 0,
    down_rc: int = 0,
    keep_db: bool = False,
    other_run_pids: tuple[int, ...] = (),
    tree: Path | None = None,
    extra_env: dict[str, str] | None = None,
    timeout: int = 60,
) -> tuple[int, list[str]]:
    if tree is None:
        tree = _stage(tmp_path, name)
    markers = tree / ".git" / "precis-test-runs"
    for pid in other_run_pids:
        markers.mkdir(exist_ok=True)
        (markers / str(pid)).write_text("", encoding="utf-8")
    env, log = _fake_docker_env(
        tmp_path,
        ps_services=ps_services,
        run_rc=run_rc,
        down_rc=down_rc,
        keep_db=keep_db,
        extra_env=extra_env,
    )
    proc = subprocess.run(
        ["bash", str(tree / "scripts" / "test"), *args],
        cwd=tree,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=timeout,
        check=False,
    )
    if proc.returncode == 0 and markers.is_dir():
        # A run never leaves its own marker behind.
        assert not [m for m in markers.iterdir() if int(m.name) not in other_run_pids]
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


def test_agent_tree_with_another_live_run_is_left_up(tmp_path: Path) -> None:
    """Round-3 review of ae5084ae3: a second run between its `compose up` and
    its own container start shows no running container, only its marker."""
    rc, log = _run(tmp_path, "agent-abc123", other_run_pids=(os.getpid(),))
    assert rc == 0
    assert not [line for line in log if line.startswith("down|")]


def test_stale_run_marker_does_not_block_teardown(tmp_path: Path) -> None:
    dead = subprocess.Popen(["true"])
    dead.wait()
    rc, log = _run(tmp_path, "agent-abc123", other_run_pids=(dead.pid,))
    assert rc == 0
    assert "down|-v|precis-test-agent-abc123" in log
    assert not (
        tmp_path / "agent-abc123" / ".git" / "precis-test-runs" / str(dead.pid)
    ).exists()


def test_fast_run_skips_the_gate_queue(tmp_path: Path) -> None:
    """--fast needs no Postgres, so it takes no gate slot (gr514466).

    The only slot is held by a live sibling; a no-DB run must still go
    straight to its container instead of queueing behind it.
    """
    tree = _stage(tmp_path, "plain-tree")
    holder = subprocess.Popen(["sleep", "60"])
    try:
        _hold_gate_slot(tree, 0, holder.pid)
        rc, log = _run(
            tmp_path,
            "plain-tree",
            args=("--fast", "-n0", "tests/x.py"),
            tree=tree,
            extra_env={"PRECIS_GATE_SLOTS": "1"},
            timeout=30,
        )
        assert rc == 0
        assert [line.split("|")[0] for line in log] == ["run"]
        qdir = tree / ".git" / "precis-gate-queue.d"
        assert not (qdir.is_dir() and list(qdir.iterdir())), (
            "a --fast run left an arrival ticket — it entered the gate queue"
        )
    finally:
        holder.terminate()
        holder.wait(timeout=10)


def test_db_run_still_queues_for_a_gate_slot(tmp_path: Path) -> None:
    """A run that starts Postgres still waits on the semaphore (gr514466).

    Same held-slot setup: the DB path must drop an arrival ticket and wait —
    only the no-DB path skips. And the wait happens BEFORE ``compose up``:
    a queued run holds no live test-db while it waits for a slot.
    """
    tree = _stage(tmp_path, "plain-tree")
    holder = subprocess.Popen(["sleep", "60"])
    env, log = _fake_docker_env(
        tmp_path,
        ps_services="precis-test-db",
        run_rc=0,
        down_rc=0,
        keep_db=False,
        extra_env={"PRECIS_GATE_SLOTS": "1"},
    )
    try:
        _hold_gate_slot(tree, 0, holder.pid)
        with (tmp_path / "stderr.log").open("w", encoding="utf-8") as err:
            proc = subprocess.Popen(
                ["bash", str(tree / "scripts" / "test"), "-n0", "tests/x.py"],
                cwd=tree,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=err,
            )
            try:
                qdir = tree / ".git" / "precis-gate-queue.d"
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline and not (
                    qdir.is_dir() and list(qdir.iterdir())
                ):
                    time.sleep(0.2)
                assert qdir.is_dir() and list(qdir.iterdir()), (
                    "a DB run never queued for the held gate slot — "
                    "the slot wait is gone from the DB path"
                )
                assert proc.poll() is None, (
                    "a DB run finished while its only slot was held"
                )
                verbs = [
                    line.split("|")[0]
                    for line in log.read_text(encoding="utf-8").splitlines()
                ]
                assert verbs == [], (
                    f"a queued DB run already started containers ({verbs}) — "
                    "the slot wait must come before `compose up`"
                )
            finally:
                proc.kill()
                proc.wait(timeout=10)
    finally:
        holder.terminate()
        holder.wait(timeout=10)

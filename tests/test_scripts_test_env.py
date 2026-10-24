"""scripts/test env forwarding — gr513868.

Only a fixed allowlist of env vars crosses into the test container
(``ARTIFACT_ENV``); ``NANO_SEEDS`` was exported on the host, silently
dropped, and a seed-iteration run measured seed 1 for four minutes.
``PRECIS_TEST_ENV="NAME=value …"`` is the opt-in for anything not on the
list, and a ``NANO_*``/``ESP32C3_*``/``PRECIS_PCB_*`` variable that is
exported but not forwarded is now named once on stderr.

These tests run the REAL ``scripts/test`` (staged byte-for-byte, with its
sourced libs, into a throwaway git repo) against a synthetic ``docker``
on PATH that logs every ``compose run`` arg vector verbatim. The real
daemon is never contacted — same harness as
``test_scripts_test_agent_teardown.py``.
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
_LIBS = ("compose-project.sh", "docker-disk.sh", "gate-slot.sh", "lock-holder.sh")

_FAKE_DOCKER = r"""#!/usr/bin/env bash
# Synthetic docker -- logs compose invocations; never touches a daemon.
#   FAKE_DOCKER_LOG   appended: up|<proj> / run|<proj>|<all args> / down|<proj>
#   FAKE_PS_SERVICES  newline-separated service names `docker ps` reports
# A plain `docker run ... df` is the free-disk preflight probe: it answers
# with 100 GB free so the preflight passes and never reaches the log.
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
      up)   echo "up|$proj" >> "$FAKE_DOCKER_LOG" ;;
      run)  echo "run|$proj|${args[*]}" >> "$FAKE_DOCKER_LOG" ;;
      down) echo "down|$proj" >> "$FAKE_DOCKER_LOG" ;;
    esac
    exit 0
    ;;
esac
exit 0
"""

_SCRUB_PREFIXES = (
    "NANO_",
    "ESP32C3_",
    "PRECIS_PCB_",
    "PRECIS_TEST_",
    "PRECIS_COMPOSE",
    "PRECIS_STRICT_LOCK_CHECK",
    "UV_WITH",
)


def _stage(tmp_path: Path, name: str = "envtest") -> Path:
    """A throwaway repo directory holding the real script."""
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
    *,
    args: tuple[str, ...] = ("-n0", "tests/x.py"),
    extra_env: dict[str, str] | None = None,
) -> tuple[int, list[str], str]:
    """Run staged scripts/test; return (rc, docker log lines, stderr)."""
    tree = _stage(tmp_path)
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir(exist_ok=True)
    docker = fakebin / "docker"
    docker.write_text(_FAKE_DOCKER, encoding="utf-8")
    docker.chmod(0o755)
    log = tmp_path / "docker.log"
    log.write_text("", encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith(_SCRUB_PREFIXES)}
    env["PATH"] = f"{fakebin}{os.pathsep}{os.environ['PATH']}"
    env["FAKE_DOCKER_LOG"] = str(log)
    env["FAKE_PS_SERVICES"] = "precis-test-db"
    env["TMPDIR"] = str(tmp_path)
    env.update(extra_env or {})
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
    return proc.returncode, log.read_text(encoding="utf-8").splitlines(), proc.stderr


def _run_line(log: list[str]) -> str:
    runs = [line for line in log if line.startswith("run|")]
    assert len(runs) == 1, f"expected exactly one compose run, got {log}"
    return runs[0]


def test_precis_test_env_forwards_pairs(tmp_path: Path) -> None:
    rc, log, stderr = _run(
        tmp_path, extra_env={"PRECIS_TEST_ENV": "NANO_SEEDS=2 FOO=bar"}
    )
    assert rc == 0, stderr
    line = _run_line(log)
    assert " -e NANO_SEEDS=2 " in f" {line} " or "-e NANO_SEEDS=2 " in line
    assert " -e FOO=bar " in f" {line} "
    assert "not forwarded" not in stderr


def test_set_but_unforwarded_tuning_var_warns_once(tmp_path: Path) -> None:
    """The gripe's failure mode verbatim: NANO_SEEDS exported, not in the
    allowlist — one stderr line naming it and the fix, and the container
    still does not see it."""
    rc, log, stderr = _run(tmp_path, extra_env={"NANO_SEEDS": "2"})
    assert rc == 0, stderr
    assert (
        "scripts/test: NANO_SEEDS is set but not forwarded into the "
        'container; use PRECIS_TEST_ENV="NANO_SEEDS=2"' in stderr
    )
    assert "NANO_SEEDS" not in _run_line(log)


def test_allowlisted_var_forwards_without_warning(tmp_path: Path) -> None:
    rc, log, stderr = _run(tmp_path, extra_env={"PRECIS_PCB_BENCH_OUT": "/tmp/x"})
    assert rc == 0, stderr
    assert " -e PRECIS_PCB_BENCH_OUT=/tmp/x " in f" {_run_line(log)} "
    assert "not forwarded" not in stderr


def test_forwarded_via_precis_test_env_suppresses_the_warning(tmp_path: Path) -> None:
    rc, log, stderr = _run(
        tmp_path,
        extra_env={"NANO_SEEDS": "3", "PRECIS_TEST_ENV": "NANO_SEEDS=3"},
    )
    assert rc == 0, stderr
    assert " -e NANO_SEEDS=3 " in f" {_run_line(log)} "
    assert "not forwarded" not in stderr


@pytest.mark.parametrize(
    "bad", ["PATH=/x", "HOME=/x", "PYTHONPATH=/x", "DATABASE_URL=p", "PGHOST=h"]
)
def test_precis_test_env_refuses_runtime_internals(tmp_path: Path, bad: str) -> None:
    rc, log, stderr = _run(tmp_path, extra_env={"PRECIS_TEST_ENV": bad})
    assert rc != 0
    assert "may not forward" in stderr
    assert not [line for line in log if line.startswith("run|")]


@pytest.mark.parametrize("bad", ["nano_seeds=2", "2FOO=x", "NOEQUALS"])
def test_precis_test_env_refuses_malformed_entries(tmp_path: Path, bad: str) -> None:
    rc, log, stderr = _run(tmp_path, extra_env={"PRECIS_TEST_ENV": bad})
    assert rc != 0
    assert "is not NAME=value" in stderr
    assert not [line for line in log if line.startswith("run|")]

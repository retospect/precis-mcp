"""scripts/dev env forwarding — gr514999 / gr513868.

``compose run`` does not cross host env into the container. scripts/dev
now exports ``PRECIS_DATABASE_URL`` at this worktree's dev DSN by default
so ``scripts/dev uv run precis …`` finds ``precis_dev`` after ``db-up``
(gr514999), honours an explicit host value with a named stderr note, and
offers ``PRECIS_DEV_ENV="NAME=value …"`` for anything else — the same
opt-in + unforwarded-warning shape scripts/test got for gr513868.

These tests run the REAL ``scripts/dev`` (staged byte-for-byte, with its
sourced lib, into a throwaway directory) against a synthetic ``docker``
on PATH that logs every ``compose run`` arg vector verbatim. The real
daemon is never contacted — same harness as ``test_scripts_test_env.py``.
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
DEV_DSN = "postgresql://postgres@precis-test-db:5432/precis_dev"

_FAKE_DOCKER = r"""#!/usr/bin/env bash
# Synthetic docker -- logs compose invocations; never touches a daemon.
#   FAKE_DOCKER_LOG   appended: run|<proj>|<all args>
set -u
args=("$@")
case "${args[0]:-}" in
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
    "PRECIS_DEV_ENV",
    "PRECIS_DATABASE_URL",
    "PRECIS_COMPOSE",
)


def _stage(tmp_path: Path, name: str = "envtest") -> Path:
    """A throwaway directory holding the real script + its sourced lib."""
    tree = tmp_path / name
    (tree / "scripts" / "lib").mkdir(parents=True)
    (tree / "docker" / "dev").mkdir(parents=True)
    shutil.copy2(REPO / "scripts" / "dev", tree / "scripts" / "dev")
    shutil.copy2(
        REPO / "scripts" / "lib" / "compose-project.sh",
        tree / "scripts" / "lib" / "compose-project.sh",
    )
    (tree / "docker" / "dev" / "compose.yaml").write_text(
        "services: {}\n", encoding="utf-8"
    )
    return tree


def _run(
    tmp_path: Path,
    *,
    args: tuple[str, ...] = ("true",),
    extra_env: dict[str, str] | None = None,
) -> tuple[int, list[str], str]:
    """Run staged scripts/dev; return (rc, docker log lines, stderr)."""
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
    env["TMPDIR"] = str(tmp_path)
    env.update(extra_env or {})
    proc = subprocess.run(
        ["bash", str(tree / "scripts" / "dev"), *args],
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


def test_database_url_defaults_to_dev_dsn(tmp_path: Path) -> None:
    """gr514999's ask: `scripts/dev uv run precis …` sees the dev DB."""
    rc, log, stderr = _run(tmp_path)
    assert rc == 0, stderr
    assert f"-e PRECIS_DATABASE_URL={DEV_DSN}" in _run_line(log)
    assert "forwarding host" not in stderr


def test_host_database_url_wins_and_is_named(tmp_path: Path) -> None:
    rc, log, stderr = _run(
        tmp_path, extra_env={"PRECIS_DATABASE_URL": "postgresql://elsewhere/db"}
    )
    assert rc == 0, stderr
    assert "-e PRECIS_DATABASE_URL=postgresql://elsewhere/db" in _run_line(log)
    assert "forwarding host PRECIS_DATABASE_URL" in stderr
    assert DEV_DSN in stderr


def test_precis_dev_env_forwards_pairs(tmp_path: Path) -> None:
    rc, log, stderr = _run(
        tmp_path, extra_env={"PRECIS_DEV_ENV": "NANO_SEEDS=2 FOO=bar"}
    )
    assert rc == 0, stderr
    line = _run_line(log)
    assert " -e NANO_SEEDS=2 " in f" {line} " or "-e NANO_SEEDS=2 " in line
    assert " -e FOO=bar " in f" {line} "
    assert "not forwarded" not in stderr


def test_set_but_unforwarded_tuning_var_warns_once(tmp_path: Path) -> None:
    """gr513868's failure mode on the dev path: exported, not forwarded —
    one stderr line naming it and the fix."""
    rc, log, stderr = _run(tmp_path, extra_env={"NANO_SEEDS": "2"})
    assert rc == 0, stderr
    assert (
        "scripts/dev: NANO_SEEDS is set but not forwarded into the "
        'container; use PRECIS_DEV_ENV="NANO_SEEDS=2"' in stderr
    )
    assert "NANO_SEEDS" not in _run_line(log)


def test_forwarded_via_precis_dev_env_suppresses_the_warning(
    tmp_path: Path,
) -> None:
    rc, log, stderr = _run(
        tmp_path,
        extra_env={"NANO_SEEDS": "3", "PRECIS_DEV_ENV": "NANO_SEEDS=3"},
    )
    assert rc == 0, stderr
    assert " -e NANO_SEEDS=3 " in f" {_run_line(log)} "
    assert "not forwarded" not in stderr


@pytest.mark.parametrize(
    "bad", ["PATH=/x", "HOME=/x", "PYTHONPATH=/x", "DATABASE_URL=p", "PGHOST=h"]
)
def test_precis_dev_env_refuses_runtime_internals(tmp_path: Path, bad: str) -> None:
    rc, log, stderr = _run(tmp_path, extra_env={"PRECIS_DEV_ENV": bad})
    assert rc != 0
    assert "may not forward" in stderr
    assert not [line for line in log if line.startswith("run|")]


@pytest.mark.parametrize("bad", ["nano_seeds=2", "2FOO=x", "NOEQUALS"])
def test_precis_dev_env_refuses_malformed_entries(tmp_path: Path, bad: str) -> None:
    rc, log, stderr = _run(tmp_path, extra_env={"PRECIS_DEV_ENV": bad})
    assert rc != 0
    assert "is not NAME=value" in stderr
    assert not [line for line in log if line.startswith("run|")]

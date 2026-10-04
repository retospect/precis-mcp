"""The docker VM disk guards: a free-space preflight and a build-cache cap.

On 2026-10-03 the colima VM disk reached 98% (90 GB build cache) and every lint,
gate and ``scripts/test`` died on a raw ENOSPC with no test output. Two guards:

* ``scripts/test`` sources ``scripts/lib/docker-disk.sh`` and refuses (exit 3,
  named message, before any compose call) below ``PRECIS_DOCKER_MIN_FREE_GB``.
  An unreadable free value only warns.
* ``scripts/reap-test-dbs`` prunes the build cache to
  ``PRECIS_BUILD_CACHE_KEEP_GB`` at most once an hour (stamp file), probing which
  keep-flag this docker names.

Both run the REAL scripts, staged into a throwaway git repo, against a synthetic
``docker`` (and ``colima``) on PATH. The real daemon is never contacted.
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
    reason="the shipped bash scripts + fake docker are POSIX-only",
)

REPO = Path(__file__).resolve().parent.parent
_LIBS = ("compose-project.sh", "docker-disk.sh", "gate-slot.sh", "lock-holder.sh")
_GB_KB = 1024 * 1024

_FAKE_DOCKER = r"""#!/usr/bin/env bash
# Synthetic docker; never touches a daemon. Appends to $FAKE_DOCKER_LOG:
#   df|<args>        a plain `docker run` (the free-disk probe)
#   up|run|down|...  compose verbs
#   prune|<args>     `docker builder prune -f <flag> <size>`
# Knobs:
#   FAKE_DF_MODE      ok (default) | fail | hang   -- the df probe's behaviour
#   FAKE_DF_FREE_KB   Available column the probe reports
#   FAKE_PRUNE_FLAG   the keep-flag `builder prune --help` advertises ('' = none)
set -u
args=("$@")
case "${args[0]:-}" in
  run)
    echo "df|${args[*]}" >> "$FAKE_DOCKER_LOG"
    case "${FAKE_DF_MODE:-ok}" in
      fail) exit 1 ;;
      hang) exec sleep 30 ;;
    esac
    printf 'Filesystem 1024-blocks Used Available Capacity Mounted on\n'
    printf 'overlay 209715200 1048576 %s 1%% /\n' "${FAKE_DF_FREE_KB:-104857600}"
    exit 0
    ;;
  builder)
    if [ "${args[1]:-}" = prune ]; then
      case " ${args[*]} " in
        *" --help "*)
          echo "Usage:  docker buildx prune"
          echo "  -f, --force   Do not prompt for confirmation"
          [ -n "${FAKE_PRUNE_FLAG:-}" ] && echo "      ${FAKE_PRUNE_FLAG} bytes   keep this much"
          exit 0
          ;;
      esac
      echo "prune|${args[*]:2}" >> "$FAKE_DOCKER_LOG"
      printf 'ID\tRECLAIMABLE\tSIZE\n'
      printf 'Total:\t12.5GB\n'
      exit 0
    fi
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
      up|run|down) echo "$verb|$proj" >> "$FAKE_DOCKER_LOG" ;;
    esac
    exit 0
    ;;
esac
exit 0
"""

# Colima is present on the maintainer's host; a fake keeps the "unknown" cases
# hermetic (the real one would answer the fallback probe).
_FAKE_COLIMA = r"""#!/usr/bin/env bash
if [ -n "${FAKE_COLIMA_FREE_KB:-}" ]; then
    printf 'Filesystem 1024-blocks Used Available Capacity Mounted on\n'
    printf '/dev/vdb1 209715200 1048576 %s 1%% /var/lib/docker\n' "$FAKE_COLIMA_FREE_KB"
    exit 0
fi
exit 1
"""


def _stage(tmp_path: Path, name: str, scripts: tuple[str, ...]) -> Path:
    tree = tmp_path / name
    (tree / "scripts" / "lib").mkdir(parents=True, exist_ok=True)
    (tree / "docker" / "dev").mkdir(parents=True, exist_ok=True)
    for script in scripts:
        shutil.copy2(REPO / "scripts" / script, tree / "scripts" / script)
    for lib in _LIBS:
        shutil.copy2(REPO / "scripts" / "lib" / lib, tree / "scripts" / "lib" / lib)
    (tree / "docker" / "dev" / "compose.yaml").write_text(
        "services: {}\n", encoding="utf-8"
    )
    subprocess.run(["git", "init", "-q", str(tree)], check=True)
    return tree


def _env(tmp_path: Path, **extra: str) -> dict[str, str]:
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir(exist_ok=True)
    for name, body in (("docker", _FAKE_DOCKER), ("colima", _FAKE_COLIMA)):
        f = fakebin / name
        f.write_text(body, encoding="utf-8")
        f.chmod(0o755)
    log = tmp_path / "docker.log"
    log.touch()
    env = {
        **os.environ,
        "PATH": f"{fakebin}{os.pathsep}{os.environ['PATH']}",
        "FAKE_DOCKER_LOG": str(log),
        "TMPDIR": str(tmp_path),
    }
    for k in (
        "PRECIS_DOCKER_MIN_FREE_GB",
        "PRECIS_DOCKER_DISK_CHECK",
        "PRECIS_DOCKER_DISK_TIMEOUT",
        "PRECIS_DOCKER_DISK_IMAGE",
        "PRECIS_BUILD_CACHE_KEEP_GB",
        "PRECIS_NO_AUTOREAP",
        "PRECIS_TEST_KEEP_DB",
        "PRECIS_COMPOSE",
    ):
        env.pop(k, None)
    env.update(extra)
    return env


def _log(tmp_path: Path) -> list[str]:
    return (tmp_path / "docker.log").read_text(encoding="utf-8").splitlines()


def _run_test(
    tmp_path: Path, *args: str, timeout: int = 60, **env: str
) -> subprocess.CompletedProcess[str]:
    tree = _stage(tmp_path, "ship-gate-ci", ("test",))
    return subprocess.run(
        ["bash", str(tree / "scripts" / "test"), *(args or ("-n0", "tests/x.py"))],
        cwd=tree,
        env=_env(tmp_path, **env),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=timeout,
        check=False,
    )


def _compose_calls(log: list[str]) -> list[str]:
    return [ln for ln in log if ln.split("|")[0] in ("up", "run", "down")]


# --- scripts/test preflight ---------------------------------------------------


def test_under_threshold_refuses_with_named_message_and_no_compose_call(
    tmp_path: Path,
) -> None:
    proc = _run_test(tmp_path, FAKE_DF_FREE_KB=str(4 * _GB_KB))
    assert proc.returncode == 3
    assert (
        "scripts/test: docker VM disk has 4 GB free; run scripts/reap-test-dbs, "
        "or ask the orchestrator to prune; do not re-run"
    ) in proc.stderr
    assert _compose_calls(_log(tmp_path)) == []


def test_threshold_is_configurable_and_exact(tmp_path: Path) -> None:
    free = str(12 * _GB_KB)
    proc = _run_test(tmp_path, FAKE_DF_FREE_KB=free, PRECIS_DOCKER_MIN_FREE_GB="13")
    assert proc.returncode == 3
    # 12 GB free against a 12 GB floor: not under it, so it runs.
    proc = _run_test(tmp_path, FAKE_DF_FREE_KB=free, PRECIS_DOCKER_MIN_FREE_GB="12")
    assert proc.returncode == 0


def test_typecheck_is_guarded_too(tmp_path: Path) -> None:
    proc = _run_test(tmp_path, "--typecheck", FAKE_DF_FREE_KB=str(2 * _GB_KB))
    assert proc.returncode == 3
    assert _compose_calls(_log(tmp_path)) == []


def test_unknown_free_space_warns_and_proceeds(tmp_path: Path) -> None:
    proc = _run_test(tmp_path, FAKE_DF_MODE="fail")
    assert proc.returncode == 0, proc.stderr
    assert "could not read the docker VM's free disk space" in proc.stderr
    assert [ln.split("|")[0] for ln in _compose_calls(_log(tmp_path))] == ["up", "run"]


def test_hung_probe_is_bounded_then_proceeds(tmp_path: Path) -> None:
    t0 = time.monotonic()
    proc = _run_test(
        tmp_path, FAKE_DF_MODE="hang", PRECIS_DOCKER_DISK_TIMEOUT="1", timeout=25
    )
    assert proc.returncode == 0, proc.stderr
    assert "could not read" in proc.stderr
    assert time.monotonic() - t0 < 15
    # A wedged daemon ends the docker leg after ONE alarm, not one per image.
    assert len([ln for ln in _log(tmp_path) if ln.startswith("df|")]) == 1


def test_colima_fallback_answers_when_docker_probe_cannot(tmp_path: Path) -> None:
    proc = _run_test(tmp_path, FAKE_DF_MODE="fail", FAKE_COLIMA_FREE_KB=str(3 * _GB_KB))
    assert proc.returncode == 3
    assert "docker VM disk has 3 GB free" in proc.stderr


def test_skip_env_var_skips_the_check(tmp_path: Path) -> None:
    proc = _run_test(
        tmp_path, FAKE_DF_FREE_KB=str(1 * _GB_KB), PRECIS_DOCKER_DISK_CHECK="0"
    )
    assert proc.returncode == 0, proc.stderr
    assert not [ln for ln in _log(tmp_path) if ln.startswith("df|")]
    assert "docker VM disk" not in proc.stderr


# --- scripts/reap-test-dbs build-cache cap ------------------------------------


def _reap(
    tmp_path: Path, *args: str, **env: str
) -> tuple[subprocess.CompletedProcess[str], Path]:
    tree = tmp_path / "main"
    if not tree.exists():
        tree = _stage(tmp_path, "main", ("reap-test-dbs",))
    proc = subprocess.run(
        ["bash", str(tree / "scripts" / "reap-test-dbs"), *args],
        cwd=tree,
        env=_env(tmp_path, **env),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    return proc, tree / ".git" / "precis-build-cache-prune.stamp"


def _prunes(tmp_path: Path) -> list[str]:
    return [ln for ln in _log(tmp_path) if ln.startswith("prune|")]


def test_prune_runs_once_and_the_stamp_suppresses_a_second_run(tmp_path: Path) -> None:
    proc, stamp = _reap(tmp_path, FAKE_PRUNE_FLAG="--reserved-space")
    assert proc.returncode == 0, proc.stderr
    assert _prunes(tmp_path) == ["prune|-f --reserved-space 30GB"]
    assert "build cache pruned to a 30GB cap, reclaimed 12.5GB" in proc.stdout
    assert stamp.is_file()

    proc2, _ = _reap(tmp_path, FAKE_PRUNE_FLAG="--reserved-space")
    assert proc2.returncode == 0
    assert len(_prunes(tmp_path)) == 1  # still one: the stamp is fresh
    assert "build cache" not in proc2.stdout


def test_stale_stamp_prunes_again(tmp_path: Path) -> None:
    _, stamp = _reap(tmp_path, FAKE_PRUNE_FLAG="--reserved-space")
    old = time.time() - 2 * 3600
    os.utime(stamp, (old, old))
    _reap(tmp_path, FAKE_PRUNE_FLAG="--reserved-space")
    assert len(_prunes(tmp_path)) == 2


@pytest.mark.parametrize(
    "flag", ["--keep-storage", "--reserved-space", "--max-used-space"]
)
def test_prune_flag_name_is_probed(tmp_path: Path, flag: str) -> None:
    proc, _ = _reap(tmp_path, FAKE_PRUNE_FLAG=flag, PRECIS_BUILD_CACHE_KEEP_GB="7")
    assert proc.returncode == 0, proc.stderr
    assert _prunes(tmp_path) == [f"prune|-f {flag} 7GB"]


def test_prune_without_any_known_flag_is_skipped_loudly(tmp_path: Path) -> None:
    proc, _ = _reap(tmp_path, FAKE_PRUNE_FLAG="")
    assert proc.returncode == 0, proc.stderr
    assert _prunes(tmp_path) == []
    assert "build-cache prune skipped" in proc.stdout


def test_dry_run_never_prunes(tmp_path: Path) -> None:
    proc, stamp = _reap(tmp_path, "--dry-run", FAKE_PRUNE_FLAG="--reserved-space")
    assert proc.returncode == 0, proc.stderr
    assert _prunes(tmp_path) == []
    assert not stamp.exists()


def test_ship_runs_the_preflight_before_any_gate_container() -> None:
    """scripts/ship's lint and gate share setup_gate_infra; the preflight must
    run there, before the compose project, so a full disk names itself."""
    ship = (REPO / "scripts" / "ship").read_text(encoding="utf-8")
    body = ship[ship.index("setup_gate_infra() {") :]
    pre = body.index('docker_disk_preflight "scripts/ship" || exit $?')
    assert pre < body.index("INFRA_COMPOSE=")

"""scripts/test forwards PRECIS_TEST_PASSENV names into the container.

gr514238: ``UNI_VIEWS=... scripts/test`` never reached pytest -- the
``ARTIFACT_ENV`` loop forwards only a fixed whitelist. ``PRECIS_TEST_PASSENV``
names (comma-separated) are appended as ``-e NAME`` next to it, so docker
inherits each value from the caller's environment and no value ever reaches
argv or a log (the vars may be secrets). An invalid name refuses before any
docker call.

These tests run the REAL ``scripts/test`` (staged byte-for-byte, with its
sourced libs) against a synthetic ``docker`` on PATH that logs the full
``compose`` argv. The real daemon is never contacted.
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
# Synthetic docker -- logs every `docker compose` argv (tab-separated) to
# FAKE_DOCKER_LOG; never touches a daemon. A plain `docker run ... df` is the
# free-disk preflight probe: it answers with 100 GB free.
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
    printf 'compose' >> "$FAKE_DOCKER_LOG"
    for a in "${args[@]:1}"; do printf '\t%s' "$a" >> "$FAKE_DOCKER_LOG"; done
    printf '\n' >> "$FAKE_DOCKER_LOG"
    for i in "${!args[@]}"; do
        case "${args[$i]}" in up|run|down) exit "${FAKE_RUN_RC:-0}" ;; esac
    done
    exit 0
    ;;
esac
exit 0
"""


def _stage(tmp_path: Path) -> Path:
    """A throwaway repo directory holding the real script and its libs."""
    tree = tmp_path / "repo"
    (tree / "scripts" / "lib").mkdir(parents=True)
    (tree / "docker" / "dev").mkdir(parents=True)
    shutil.copy2(REPO / "scripts" / "test", tree / "scripts" / "test")
    for lib in _LIBS:
        shutil.copy2(REPO / "scripts" / "lib" / lib, tree / "scripts" / "lib" / lib)
    (tree / "docker" / "dev" / "compose.yaml").write_text(
        "services: {}\n", encoding="utf-8"
    )
    subprocess.run(["git", "init", "-q", str(tree)], check=True)
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir(exist_ok=True)
    docker = fakebin / "docker"
    docker.write_text(_FAKE_DOCKER, encoding="utf-8")
    docker.chmod(0o755)
    return tree


def _run(
    tmp_path: Path,
    tree: Path,
    *,
    log_name: str = "docker.log",
    args: tuple[str, ...] = ("--fast", "-n0", "tests/x.py"),
    extra_env: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], list[list[str]]]:
    """Returns (proc, compose argv lists) for one run of the staged script."""
    log = tmp_path / log_name
    log.write_text("", encoding="utf-8")
    env = {
        **os.environ,
        "PATH": f"{tmp_path / 'fakebin'}{os.pathsep}{os.environ['PATH']}",
        "FAKE_DOCKER_LOG": str(log),
        "FAKE_RUN_RC": "0",
        "TMPDIR": str(tmp_path),
    }
    env.pop("PRECIS_TEST_PASSENV", None)
    env.pop("PRECIS_TEST_KEEP_DB", None)
    env.pop("PRECIS_COMPOSE", None)
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
    calls = [
        line.split("\t")[1:]
        for line in log.read_text(encoding="utf-8").splitlines()
        if line.startswith("compose\t")
    ]
    return proc, calls


def _run_argv(calls: list[list[str]]) -> list[str]:
    """The argv of the single ``compose run`` invocation."""
    runs = [c for c in calls if "run" in c]
    assert len(runs) == 1, f"expected one compose run, got {calls}"
    argv = runs[0]
    return argv[argv.index("run") + 1 :]


def test_passenv_names_forwarded_as_bare_e(tmp_path: Path) -> None:
    proc, calls = _run(
        tmp_path,
        _stage(tmp_path),
        extra_env={
            "PRECIS_TEST_PASSENV": "UNI_VIEWS,MY_FLAG",
            "UNI_VIEWS": "tree,drc",
            "MY_FLAG": "1",
        },
    )
    assert proc.returncode == 0, proc.stderr
    argv = _run_argv(calls)
    # `-e NAME` twice, as separate tokens -- docker resolves the value.
    for name in ("UNI_VIEWS", "MY_FLAG"):
        i = argv.index(name)
        assert argv[i - 1] == "-e"
    # Values never reach argv (or logs): the vars may be secrets.
    assert not [a for a in argv if "tree,drc" in a or a in ("1", "MY_FLAG=1")]


def test_passenv_unset_leaves_argv_byte_identical(tmp_path: Path) -> None:
    tree = _stage(tmp_path)
    _, off_calls = _run(tmp_path, tree, log_name="off.log")
    _, on_calls = _run(
        tmp_path,
        tree,
        log_name="on.log",
        extra_env={"PRECIS_TEST_PASSENV": "FOO,BAR", "FOO": "x", "BAR": "y"},
    )
    off_argv = _run_argv(off_calls)
    on_argv = _run_argv(on_calls)
    # The on-run argv is the unset-run argv plus exactly `-e FOO -e BAR`,
    # inserted next to ARTIFACT_ENV right before the `-v` mount.
    i = off_argv.index("-v")
    assert on_argv == off_argv[:i] + ["-e", "FOO", "-e", "BAR"] + off_argv[i:]


@pytest.mark.parametrize("bad", ["9BAD", "FOO-BAR", "FOO=bar", "FOO BAR"])
def test_invalid_name_refused_before_docker(tmp_path: Path, bad: str) -> None:
    proc, calls = _run(
        tmp_path, _stage(tmp_path), extra_env={"PRECIS_TEST_PASSENV": f"OK,{bad}"}
    )
    assert proc.returncode == 1
    assert f"'{bad}'" in proc.stderr
    assert "next:" in proc.stderr
    assert calls == []  # refused before any docker call


def test_unset_named_var_warns_name_only(tmp_path: Path) -> None:
    proc, calls = _run(
        tmp_path,
        _stage(tmp_path),
        extra_env={"PRECIS_TEST_PASSENV": "SET_VAR,GONE_VAR", "SET_VAR": "sekrit"},
    )
    assert proc.returncode == 0, proc.stderr
    assert "GONE_VAR" in proc.stderr
    assert "sekrit" not in proc.stderr + proc.stdout
    argv = _run_argv(calls)
    i = argv.index("GONE_VAR")
    assert argv[i - 1] == "-e"  # still forwarded; docker drops it as absent

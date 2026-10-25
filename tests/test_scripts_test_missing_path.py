"""scripts/test missing-path preflight (gr514948).

One bad filename used to reach pytest, which collected nothing and reported
a green-looking "no tests ran" (exit 5) — silently voiding the whole
verification run. scripts/test now refuses (exit 2) before the disk probe,
test-db and container start, naming every positional arg that is not an
existing path; a ``path::node`` id checks only its path part. Args following
a value-taking flag (``-k``/``-m`` expressions, ``--junitxml``'s not-yet-
written output) are not path-checked, so pytest's own arguments still work.

Runs the REAL script staged into a throwaway repo against a synthetic
``docker`` on PATH — the same rig as tests/test_scripts_test_env.py, whose
``_stage``/``_FAKE_DOCKER`` this reuses.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.test_scripts_test_env import _FAKE_DOCKER, _stage

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="the shipped bash script is POSIX-only"
)


def _run(
    tmp_path: Path,
    *args: str,
    files: tuple[str, ...] = ("tests/test_a.py",),
) -> tuple[subprocess.CompletedProcess[str], str]:
    tree = _stage(tmp_path)
    for rel in files:
        p = tree / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("def test_stub():\n    pass\n", encoding="utf-8")
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
        "FAKE_PS_SERVICES": "precis-test-db",
        "TMPDIR": str(tmp_path),
    }
    for k in ("PRECIS_TEST_ENV", "PRECIS_TEST_KEEP_DB", "PRECIS_COMPOSE"):
        env.pop(k, None)
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
    return proc, log.read_text(encoding="utf-8")


def test_missing_file_is_named_before_any_container(tmp_path: Path) -> None:
    proc, docker_log = _run(tmp_path, "-n0", "tests/test_a.py", "tests/test_missing.py")
    assert proc.returncode == 2
    assert "test path not found: tests/test_missing.py" in proc.stderr
    assert "test_a.py" not in proc.stderr
    # The refusal precedes even the disk probe's docker run.
    assert docker_log == ""


def test_missing_dir_is_named(tmp_path: Path) -> None:
    proc, docker_log = _run(tmp_path, "tests/test_a.py", "tests/nope/")
    assert proc.returncode == 2
    assert "test path not found: tests/nope/" in proc.stderr
    assert docker_log == ""


def test_bare_missing_name_is_refused(tmp_path: Path) -> None:
    """A positional arg is always a path to pytest — a bare word that
    doesn't exist is a typo'd path, not an expression."""
    proc, _ = _run(tmp_path, "-n0", "unit")
    assert proc.returncode == 2
    assert "test path not found: unit" in proc.stderr


def test_missing_file_in_a_node_id_is_named(tmp_path: Path) -> None:
    proc, _ = _run(tmp_path, "tests/test_missing.py::test_stub")
    assert proc.returncode == 2
    assert "test path not found: tests/test_missing.py::test_stub" in proc.stderr


def test_node_id_on_an_existing_file_runs(tmp_path: Path) -> None:
    proc, docker_log = _run(tmp_path, "-n0", "tests/test_a.py::test_stub")
    assert proc.returncode == 0, proc.stderr
    assert "run|" in docker_log


def test_k_and_m_expressions_are_not_path_checked(tmp_path: Path) -> None:
    proc, _ = _run(
        tmp_path, "-n0", "-k", "stub or other", "-m", "not slow", "tests/test_a.py"
    )
    assert proc.returncode == 0, proc.stderr


def test_output_flag_values_are_not_path_checked(tmp_path: Path) -> None:
    """--junitxml's path legitimately does not exist yet — it is an output."""
    proc, _ = _run(tmp_path, "-n0", "--junitxml", "out/result.xml", "tests/test_a.py")
    assert proc.returncode == 0, proc.stderr


def test_a_missing_path_under_impacted_is_still_refused(tmp_path: Path) -> None:
    proc, docker_log = _run(tmp_path, "--impacted", "-n0", "tests/test_missing.py")
    assert proc.returncode == 2
    assert "test path not found: tests/test_missing.py" in proc.stderr
    assert docker_log == ""

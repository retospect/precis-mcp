"""Serial-run paths — gr514909, gr514400.

gr514909: ``uv run python -m pytest -p no:xdist`` used to die at arg parse —
pyproject's ``addopts = "-n auto --max-worker-restart=0"`` names options
that stop existing the moment the plugin unloads ("unrecognized arguments:
-n"), so no serial host run was possible. tests/conftest.py's
``pytest_addoption`` now registers no-op stand-ins for ``-n``,
``--numprocesses`` and ``--max-worker-restart`` when no ``xdist`` plugin is
loaded, so the parse succeeds and the run goes single-process. addopts
keeps ``-n auto``: check.yml's shard legs rely on it for parallelism.

gr514400 (scripts/test half): the same collision inside the container —
the script's injected ``-n6`` met the same dead options. The conftest shim
absorbs it (and ``scripts/test`` also stops injecting ``-n`` when the
caller disabled xdist); ``scripts/test --serial`` is the documented serial
flag, forwarding ``-n0``.

The pytest-level tests stage a minimal project (the repo's own conftest
copied into a throwaway tree with the same addopts) and run a REAL
subprocess pytest — the shim only proves itself at arg-parse time, which a
unit test cannot reach. The scripts/test half reuses the fake-docker rig
from tests/test_scripts_test_env.py.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.test_scripts_test_env import _run as _scripts_test_run
from tests.test_scripts_test_env import _run_line

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="the shipped bash script is POSIX-only"
)

REPO = Path(__file__).resolve().parent.parent


def _stage_pytest_tree(tmp_path: Path) -> Path:
    """A minimal project carrying the repo conftest and the same addopts."""
    tree = tmp_path / "serialproj"
    (tree / "tests").mkdir(parents=True)
    for name in ("conftest.py", "__init__.py", "_node.py"):
        shutil.copy2(REPO / "tests" / name, tree / "tests" / name)
    (tree / "tests" / "test_tiny.py").write_text(
        "def test_ok():\n    pass\n", encoding="utf-8"
    )
    (tree / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\n"
        'addopts = "-n auto --max-worker-restart=0"\n'
        'testpaths = ["tests"]\n',
        encoding="utf-8",
    )
    return tree


def _pytest(tree: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env.pop("PRECIS_TEST_PG_URL", None)
    return subprocess.run(
        [sys.executable, "-m", "pytest", *args],
        cwd=tree,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        check=False,
    )


def test_no_xdist_run_parses_addopts_and_runs_serial(tmp_path: Path) -> None:
    """The gr514909 command verbatim: ``pytest -p no:xdist`` must reach the
    tests, not die at "unrecognized arguments: -n"."""
    tree = _stage_pytest_tree(tmp_path)
    proc = _pytest(tree, "-p", "no:xdist", "-q", "tests/test_tiny.py")
    assert "unrecognized arguments" not in proc.stderr
    assert proc.returncode == 0, proc.stderr
    assert "1 passed" in proc.stderr + proc.stdout


def test_no_xdist_compact_p_flag_also_works(tmp_path: Path) -> None:
    """``-pno:xdist`` (no space) is the same disable spell."""
    tree = _stage_pytest_tree(tmp_path)
    proc = _pytest(tree, "-pno:xdist", "-q", "tests/test_tiny.py")
    assert proc.returncode == 0, proc.stderr
    assert "unrecognized arguments" not in proc.stderr


def test_normal_run_still_uses_real_xdist_options(tmp_path: Path) -> None:
    """When xdist IS loaded the shim must not register — ``-n0`` has to be
    the plugin's own flag, so a duplicate registration here would red the
    run."""
    tree = _stage_pytest_tree(tmp_path)
    proc = _pytest(tree, "-n0", "-q", "tests/test_tiny.py")
    assert proc.returncode == 0, proc.stderr
    assert "1 passed" in proc.stderr + proc.stdout


# --- scripts/test --serial + -p no:xdist passthrough (gr514400) ----------


def test_serial_flag_forwards_n0_not_n6(tmp_path: Path) -> None:
    rc, log, stderr = _scripts_test_run(
        tmp_path, args=("--serial", "tests/x.py"), files=("tests/x.py",)
    )
    assert rc == 0, stderr
    line = _run_line(log)
    assert " -n0 " in f" {line} "
    assert " -n6 " not in f" {line} "


def test_no_xdist_suppresses_injected_n6(tmp_path: Path) -> None:
    """``-p no:xdist`` unloads the plugin — the script must not inject its
    own ``-n6`` either (the conftest shim absorbs addopts' ``-n auto``; the
    run is serial throughout)."""
    rc, log, stderr = _scripts_test_run(
        tmp_path, args=("-p", "no:xdist", "tests/x.py"), files=("tests/x.py",)
    )
    assert rc == 0, stderr
    line = _run_line(log)
    assert " -n6 " not in f" {line} "
    assert " no:xdist " in f" {line} "

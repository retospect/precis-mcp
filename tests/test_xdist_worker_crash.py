"""A crashed (OOM-killed) xdist worker stops the run with one failure (gr346941).

xdist's default replaces a dead worker. On the 2026-09-18 ``--mutate`` gate the
replacement ran under ``--cov-context=test`` and errored every test it was
handed (1420 ERRORs + INTERNALERROR), burying the one real event. The repo's
``addopts`` pass ``--max-worker-restart=0`` so the run stops at the crash.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def test_addopts_disable_worker_restart() -> None:
    cfg = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    assert "--max-worker-restart=0" in cfg["tool"]["pytest"]["ini_options"]["addopts"]


def test_killed_worker_is_one_failure_not_a_wall_of_errors(tmp_path: Path) -> None:
    (tmp_path / "test_die.py").write_text(
        textwrap.dedent(
            """
            import os
            import pytest

            @pytest.mark.parametrize("i", range(40))
            def test_x(i):
                if i == 5:
                    os._exit(9)
            """
        ),
        encoding="utf-8",
    )
    # Reuse the repo's real addopts so the test follows the config.
    cfg = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    addopts = cfg["tool"]["pytest"]["ini_options"]["addopts"].split()
    flags = [a for a in addopts if a.startswith("--max-worker-restart")]
    proc = subprocess.run(
        [
            sys.executable, "-m", "pytest", "-p", "no:cacheprovider",
            "--rootdir", str(tmp_path), "-c", "/dev/null",
            "-n", "2", *flags, "-q", str(tmp_path / "test_die.py"),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        cwd=tmp_path,
    )  # fmt: skip
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0
    summary = out.strip().splitlines()[-1]
    assert summary.startswith("1 failed, "), out
    assert "error" not in summary, out
    # Stopped at the crash: the 40 params are not all run on a replacement.
    assert "replacing crashed worker" not in out, out
    passed = int(summary.split(", ")[1].split()[0])
    assert passed < 39, out

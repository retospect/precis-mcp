"""``deploy/playbooks/files/precis-agent-build.sh`` — the agent-image build's
progress watchdog.

Runs the REAL script against fake build commands, the same shape as
``tests/test_deploy_render_worktree.py`` runs the real ``scripts/deploy``: the
value of a watchdog is entirely in what it does at the boundary (silence vs
slow vs failed), and a reimplementation in Python would test the
reimplementation.

The script's production poll interval is 15s; every test here drives it to 1s
via ``PRECIS_AGENT_BUILD_POLL_SEC`` so the stall cases finish in seconds.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX-only: execs a shebang'd repo shell script via bash",
)

SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "deploy"
    / "playbooks"
    / "files"
    / "precis-agent-build.sh"
)


def _run(
    log: Path, stall_sec: int, *cmd: str, timeout: int = 60
) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PRECIS_AGENT_BUILD_POLL_SEC": "1"}
    return subprocess.run(
        [str(SCRIPT), str(log), str(stall_sec), *cmd],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=timeout,
        env=env,
    )


def test_the_script_is_executable_and_parses() -> None:
    assert SCRIPT.exists(), f"{SCRIPT} is missing"
    assert os.access(SCRIPT, os.X_OK), f"{SCRIPT} is not executable"
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)


def test_too_few_arguments_is_a_usage_error(tmp_path: Path) -> None:
    proc = subprocess.run(
        [str(SCRIPT), str(tmp_path / "log")],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert proc.returncode == 2
    assert "usage:" in proc.stderr


def test_a_fast_build_passes_through_its_output_and_exit_code(tmp_path: Path) -> None:
    log = tmp_path / "build.log"
    proc = _run(log, 30, "bash", "-c", "echo '#5 building'; exit 0")

    assert proc.returncode == 0
    assert "BUILD_STALLED" not in proc.stdout
    assert "BUILD_ELAPSED_SEC=" in proc.stdout
    body = log.read_text(encoding="utf-8")
    # The build's own output lands in the host-side log, not on stdout —
    # that is what survives an outer async kill.
    assert "#5 building" in body
    assert "done rc=0" in body


def test_a_failing_build_keeps_its_own_exit_code(tmp_path: Path) -> None:
    """A real failure must stay distinguishable from a watchdog kill (124)."""
    log = tmp_path / "build.log"
    proc = _run(log, 30, "bash", "-c", "echo 'ERROR: no space left'; exit 1")

    assert proc.returncode == 1
    assert "BUILD_STALLED" not in proc.stdout
    assert "ERROR: no space left" in log.read_text(encoding="utf-8")


def test_a_silent_build_is_killed_and_reported_as_stalled(tmp_path: Path) -> None:
    log = tmp_path / "build.log"
    # Emits one line, then goes quiet for far longer than the ceiling — the
    # signature of a wedged registry fetch, which burns no CPU and stays
    # alive, so process liveness would call this healthy.
    proc = _run(log, 3, "bash", "-c", "echo '#3 [2/9] FROM python:3.12'; sleep 120")

    assert proc.returncode == 124, "a watchdog kill must exit 124, not the cmd's code"
    assert "BUILD_STALLED=1" in proc.stdout
    body = log.read_text(encoding="utf-8")
    assert "WATCHDOG" in body
    # The last step before the silence is the whole point of the capture.
    assert "#3 [2/9] FROM python:3.12" in body


def test_a_slow_but_talking_build_is_not_killed(tmp_path: Path) -> None:
    """The discriminator is silence, not elapsed time.

    This build runs well past the stall ceiling but keeps writing, which is
    what a legitimate cold build (43 min, measured on a cluster node) looks
    like. Killing it would be the regression.
    """
    log = tmp_path / "build.log"
    proc = _run(
        log,
        3,
        "bash",
        "-c",
        'for i in 1 2 3 4 5 6 7 8; do echo "#$i step"; sleep 1; done',
    )

    assert proc.returncode == 0
    assert "BUILD_STALLED" not in proc.stdout
    assert "WATCHDOG" not in log.read_text(encoding="utf-8")


def test_the_stall_flag_file_is_not_left_behind(tmp_path: Path) -> None:
    """A leftover flag would make the NEXT build report a phantom stall."""
    log = tmp_path / "build.log"
    _run(log, 3, "bash", "-c", "sleep 120")

    assert not Path(f"{log}.stalled").exists()


@pytest.mark.parametrize("stall_sec", [3, 4])
def test_elapsed_is_reported_on_both_paths(tmp_path: Path, stall_sec: int) -> None:
    """Every run records its duration, stalled or not.

    The three 2026-09-26 incidents were timed by hand after the fact; this is
    what turns that into a per-deploy record.
    """
    ok = tmp_path / f"ok-{stall_sec}.log"
    stalled = tmp_path / f"stalled-{stall_sec}.log"

    ok_proc = _run(ok, stall_sec, "bash", "-c", "echo hi")
    stalled_proc = _run(stalled, stall_sec, "bash", "-c", "sleep 120")

    for proc in (ok_proc, stalled_proc):
        assert "BUILD_ELAPSED_SEC=" in proc.stdout
    assert "elapsed=" in ok.read_text(encoding="utf-8")
    assert "elapsed=" in stalled.read_text(encoding="utf-8")

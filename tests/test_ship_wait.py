"""``scripts/ship-wait`` — the one gate waiter (gr515152).

The watcher runs in the background and prints nothing until the gate
resolves: a verdict line, plus the first failure line on red. Log mode
reads the markers scripts/ship and scripts/release already print
(``SHIP_STATUS:``, ``✖``, ``remote gate GREEN``, ``CI green/red on``,
``RELEASE_EXIT=N``); run-id mode polls ``gh run view`` — stubbed on PATH
here. Fixture logs cover green, red and cancelled; nothing waits on the
real network.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "ship-wait"

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="the gh stub is a POSIX sh script"
)

GREEN_LOG = """\
▶ remote gate — pushing ci/feat-x, waiting for check.yml on abc12345
check run 223344 → https://example.invalid/run/223344
▶ remote gate GREEN (run 223344)
▶ squash-merging to main
landed on main
deadbeef123456
full log: /tmp/ship-x.log
SHIP_STATUS: shipped
"""

RED_LOG = """\
▶ remote gate — pushing ci/feat-x, waiting for check.yml on abc12345
check run 223355 → https://example.invalid/run/223355
✖ remote gate RED: failure — failing jobs:
  test-linux (3.13, 5): failure
  lint: success
  → gh run view 223355 --log-failed
full log: /tmp/ship-x.log
SHIP_STATUS: error
"""

CANCELLED_LOG = """\
▶ remote gate — pushing ci/feat-x, waiting for check.yml on abc12345
check run 223366 → https://example.invalid/run/223366
▶ previous run 223366 was cancelled — rerunning check.yml on the same sha
✖ remote gate stayed cancelled after 2 rerun attempts (run 223366)
full log: /tmp/ship-x.log
SHIP_STATUS: error
"""

CONFLICT_LOG = """\
✖ rebase conflict onto origin/main — resolve, then re-run scripts/ship
conflicted files:
src/precis/x.py
full log: /tmp/ship-x.log
SHIP_STATUS: conflict
"""

RELEASE_GREEN_LOG = """\
waiting for CI on release/r42
CI green on release/r42 at deadbeef1
RELEASE_EXIT=0
"""

RELEASE_RED_LOG = """\
waiting for CI on release/r42
CI red on release/r42 at deadbeef1 — failing: test-linux (3.13, 5)
  gh run view 223377 --log-failed
RELEASE_EXIT=2
"""

_GH_STUB = """\
#!/usr/bin/env python3
import os
import sys
from pathlib import Path

state = Path(os.environ["GH_STUB_STATE"])
if "jobs" in sys.argv:
    print(state.with_suffix(".jobs").read_text(encoding="utf-8"), end="")
else:
    print(state.read_text(encoding="utf-8"), end="")
"""


def _gh_stub(tmp_path: Path, status: str, jobs: str = "") -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    gh = bin_dir / "gh"
    gh.write_text(_GH_STUB, encoding="utf-8")
    gh.chmod(0o755)
    (tmp_path / "ghstate").write_text(status, encoding="utf-8")
    (tmp_path / "ghstate.jobs").write_text(jobs, encoding="utf-8")
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    env["GH_STUB_STATE"] = str(tmp_path / "ghstate")
    env["PRECIS_SHIP_WAIT_INTERVAL"] = "0"
    return env


def _run(
    args: list[str], env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env or {**os.environ, "PRECIS_SHIP_WAIT_INTERVAL": "0"},
        check=False,
        timeout=60,
    )


def _log(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "ship.log"
    p.write_text(text, encoding="utf-8")
    return p


def test_green_ship_log_exits_zero(tmp_path: Path) -> None:
    cp = _run([str(_log(tmp_path, GREEN_LOG))])
    assert cp.returncode == 0, cp.stdout + cp.stderr
    assert "ship-wait: GREEN" in cp.stdout


def test_green_ship_log_before_epilogue_also_exits_zero(tmp_path: Path) -> None:
    """A watch on ship's full transcript sees `remote gate GREEN` long
    before the outer log's SHIP_STATUS token — the gate verdict is the
    waiter's answer either way."""
    cp = _run([str(_log(tmp_path, GREEN_LOG.split("full log")[0]))])
    assert cp.returncode == 0
    assert "remote gate GREEN" in cp.stdout


def test_red_ship_log_names_first_failure_and_pointer(tmp_path: Path) -> None:
    cp = _run([str(_log(tmp_path, RED_LOG))])
    assert cp.returncode == 1
    assert "ship-wait: RED" in cp.stdout
    assert "first failure: test-linux (3.13, 5): failure" in cp.stdout
    assert "gh run view 223355 --log-failed" in cp.stdout


def test_cancelled_ship_log_exits_nonzero(tmp_path: Path) -> None:
    cp = _run([str(_log(tmp_path, CANCELLED_LOG))])
    assert cp.returncode != 0
    assert "CANCELLED" in cp.stdout


def test_conflict_status_is_red(tmp_path: Path) -> None:
    cp = _run([str(_log(tmp_path, CONFLICT_LOG))])
    assert cp.returncode == 1
    assert "SHIP_STATUS: conflict" in cp.stdout


def test_release_exit_zero_is_green(tmp_path: Path) -> None:
    cp = _run([str(_log(tmp_path, RELEASE_GREEN_LOG))])
    assert cp.returncode == 0


def test_release_exit_nonzero_is_red(tmp_path: Path) -> None:
    cp = _run([str(_log(tmp_path, RELEASE_RED_LOG))])
    assert cp.returncode == 1
    assert "RELEASE_EXIT=2" in cp.stdout
    assert "gh run view 223377 --log-failed" in cp.stdout


def test_run_id_green(tmp_path: Path) -> None:
    cp = _run(["998877"], env=_gh_stub(tmp_path, "completed success"))
    assert cp.returncode == 0
    assert "ship-wait: GREEN" in cp.stdout


def test_run_id_red_names_first_failing_job(tmp_path: Path) -> None:
    env = _gh_stub(
        tmp_path,
        "completed failure",
        "test-linux (3.13, 5): failure\nmypy: failure\n",
    )
    cp = _run(["998877"], env=env)
    assert cp.returncode == 1
    assert "first failure: test-linux (3.13, 5): failure" in cp.stdout
    assert "gh run view 998877 --log-failed" in cp.stdout


def test_run_id_cancelled_exits_nonzero(tmp_path: Path) -> None:
    cp = _run(["998877"], env=_gh_stub(tmp_path, "completed cancelled"))
    assert cp.returncode != 0
    assert "CANCELLED" in cp.stdout


def test_timeout_with_no_marker(tmp_path: Path) -> None:
    cp = _run(
        [str(_log(tmp_path, "still running\n")), "--timeout-min", "0"],
    )
    assert cp.returncode == 4
    assert "TIMEOUT" in cp.stdout


def test_missing_log_times_out_fast(tmp_path: Path) -> None:
    cp = _run(
        [str(tmp_path / "never.log")],
        env={
            **os.environ,
            "PRECIS_SHIP_WAIT_INTERVAL": "0.05",
            "PRECIS_SHIP_WAIT_APPEAR_S": "0.3",
        },
    )
    assert cp.returncode == 4
    assert "never appeared" in cp.stdout

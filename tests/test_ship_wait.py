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

import json
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
✖ remote gate RED (run 223355):
  test-linux (3.13, 5): failure
  → scripts/ci-failed 223355
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
  scripts/ci-failed 223377
RELEASE_EXIT=2
"""

_GH_STUB = """\
#!/usr/bin/env python3
import os
import sys
from pathlib import Path

state = Path(os.environ["GH_STUB_STATE"])
path = state.with_suffix(".jobs") if "--jq" in sys.argv else state
print(path.read_text(encoding="utf-8"), end="")
"""


def _job(
    name: str, status: str = "completed", conclusion: str | None = "success"
) -> dict:
    return {"name": name, "status": status, "conclusion": conclusion}


def _gh_stub(
    tmp_path: Path,
    status: str,
    jobs: str = "",
    run_jobs: list[dict] | None = None,
) -> dict[str, str]:
    """`gh run view` on PATH: `ghstate` holds the run JSON (`status` is
    "<status> [<conclusion>]"), `ghstate.jobs` the --jq'd failure lines."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    gh = bin_dir / "gh"
    gh.write_text(_GH_STUB, encoding="utf-8")
    gh.chmod(0o755)
    head, _, conclusion = status.partition(" ")
    run = {"status": head, "conclusion": conclusion or None, "jobs": run_jobs or []}
    (tmp_path / "ghstate").write_text(json.dumps(run), encoding="utf-8")
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
    assert "scripts/ci-failed 223355" in cp.stdout


def test_red_ship_log_passes_through_the_old_log_failed_pointer(
    tmp_path: Path,
) -> None:
    """Logs written before `scripts/ci-failed` still carry
    `gh run view <id> --log-failed` — the pointer regex picks up both
    spellings (gr514904)."""
    log = RED_LOG.replace("scripts/ci-failed 223355", "gh run view 223355 --log-failed")
    cp = _run([str(_log(tmp_path, log))])
    assert cp.returncode == 1
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
    assert "scripts/ci-failed 223377" in cp.stdout


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
    assert "scripts/ci-failed 998877" in cp.stdout


def test_run_id_cancelled_exits_nonzero(tmp_path: Path) -> None:
    cp = _run(["998877"], env=_gh_stub(tmp_path, "completed cancelled"))
    assert cp.returncode != 0
    assert "CANCELLED" in cp.stdout


def test_run_id_green_while_soak_still_runs(tmp_path: Path) -> None:
    """gr581652: the deciding set's green IS the verdict — a still-running
    continue-on-error `soak` leg never delays it."""
    env = _gh_stub(
        tmp_path,
        "in_progress",
        run_jobs=[
            _job("plan"),
            _job("lint"),
            _job("test-linux (3.13, 5)"),
            _job("soak", "in_progress", None),
        ],
    )
    cp = _run(["998877"], env=env)
    assert cp.returncode == 0
    assert "ship-wait: GREEN" in cp.stdout


def test_run_id_deciding_job_red_despite_running_soak(tmp_path: Path) -> None:
    """A red deciding job is the verdict whatever the soak does."""
    env = _gh_stub(
        tmp_path,
        "in_progress",
        run_jobs=[
            _job("plan"),
            _job("lint"),
            _job("test-linux (3.13, 5)", conclusion="failure"),
            _job("soak", "in_progress", None),
        ],
    )
    cp = _run(["998877"], env=env)
    assert cp.returncode == 1
    assert "first failure: test-linux (3.13, 5): failure" in cp.stdout


def test_run_id_red_soak_does_not_hold_the_gate(tmp_path: Path) -> None:
    """A failed soak leg is recorded, not gated on (me580555): the
    deciding set's green still answers GREEN."""
    env = _gh_stub(
        tmp_path,
        "in_progress",
        run_jobs=[
            _job("plan"),
            _job("lint"),
            _job("test-linux (3.13, 1)"),
            _job("soak", conclusion="failure"),
        ],
    )
    cp = _run(["998877"], env=env)
    assert cp.returncode == 0
    assert "ship-wait: GREEN" in cp.stdout


def test_run_id_gate_fast_counts_as_deciding(tmp_path: Path) -> None:
    """gate-s2's `gate-fast` leg is deciding before it exists in check.yml —
    the name lands with the soak matrix (gr581652)."""
    env = _gh_stub(
        tmp_path,
        "in_progress",
        run_jobs=[
            _job("plan"),
            _job("gate-fast", conclusion="failure"),
            _job("soak", "in_progress", None),
        ],
    )
    cp = _run(["998877"], env=env)
    assert cp.returncode == 1
    assert "first failure: gate-fast: failure" in cp.stdout


def test_run_id_unsettled_deciding_job_keeps_polling(tmp_path: Path) -> None:
    """A deciding job still in flight is not a verdict — the waiter keeps
    polling until it (or the run) settles."""
    env = _gh_stub(
        tmp_path,
        "in_progress",
        run_jobs=[
            _job("plan"),
            _job("lint", "in_progress", None),
            _job("soak", "in_progress", None),
        ],
    )
    cp = _run(["998877", "--timeout-min", "0"], env=env)
    assert cp.returncode == 4
    assert "TIMEOUT" in cp.stdout


def test_once_reports_a_settled_verdict(tmp_path: Path) -> None:
    """`--once` is one poll of the same deciding-set verdict — the call a
    bash wait loop (ship's remote gate) makes per tick (gr581652)."""
    env = _gh_stub(
        tmp_path,
        "in_progress",
        run_jobs=[
            _job("plan"),
            _job("lint"),
            _job("soak", "in_progress", None),
        ],
    )
    cp = _run(["998877", "--once"], env=env)
    assert cp.returncode == 0
    assert "ship-wait: GREEN" in cp.stdout


def test_once_exits_pending_while_undecided(tmp_path: Path) -> None:
    env = _gh_stub(
        tmp_path,
        "in_progress",
        run_jobs=[_job("lint", "in_progress", None)],
    )
    cp = _run(["998877", "--once"], env=env)
    assert cp.returncode == 5


def test_once_needs_a_run_id(tmp_path: Path) -> None:
    cp = _run(["some.log", "--once"])
    assert cp.returncode == 2


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

"""``remote_gate_wait`` waits on the deciding-set verdict (gr581652).

Each tick the poll loop asks ``scripts/ship-wait --once`` for the run's
verdict: GREEN the moment the deciding jobs — plan, lint, failed-first,
the test-linux/test-docs shards and gate-fast — settle green, even while
a continue-on-error soak leg still runs or records a failure; RED the
moment one of them fails; a cancelled run gets a bounded in-place rerun
before the gate gives up. Whole-run ``status,conclusion`` never decided
any of that — an in-progress run with a green deciding set is GREEN.

The function is cut out of the real ``scripts/ship`` and run under bash
with a stubbed ``gh`` on PATH and a ``scripts/ship-wait`` shim in the
scratch repo that execs the real helper under this python — no network,
no GitHub.

The ``scripts/ship`` half itself lands on a harness-authorized lane, so
this module skips while the extracted function still polls whole-run
``status,conclusion`` — the coverage activates with that land rather
than racing or ossifying the poll it replaces.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

SHIP = Path(__file__).resolve().parents[1] / "scripts" / "ship"
SHIP_WAIT = Path(__file__).resolve().parents[1] / "scripts" / "ship-wait"


def _remote_gate_function() -> str:
    source = SHIP.read_text(encoding="utf-8")
    match = re.search(r"^remote_gate_wait\(\) \{\n.*?^\}\n", source, re.S | re.M)
    assert match, "remote_gate_wait not found in scripts/ship"
    return match.group(0)


pytestmark = [
    pytest.mark.skipif(
        sys.platform == "win32", reason="the shipped bash script is POSIX-only"
    ),
    pytest.mark.skipif(
        "scripts/ship-wait" not in _remote_gate_function(),
        reason="remote_gate_wait does not call scripts/ship-wait yet — "
        "the scripts/ship half lands on a harness-authorized lane",
    ),
]


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )


def _repo(tmp_path: Path) -> Path:
    origin = tmp_path / "origin.git"
    repo = tmp_path / "repo"
    origin.mkdir()
    repo.mkdir()
    _git(origin, "init", "--bare", "-q")
    _git(repo, "init", "-q", "-b", "test")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")
    (repo / "README").write_text("x\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "-q", "origin", "HEAD:refs/heads/ci/test")
    # The gate calls `scripts/ship-wait` relative to the repo root; the shim
    # runs the real helper under this python (no uv, no network).
    shim = repo / "scripts" / "ship-wait"
    shim.parent.mkdir()
    shim.write_text(
        f'#!/bin/sh\nexec "{sys.executable}" "{SHIP_WAIT}" "$@"\n',
        encoding="utf-8",
    )
    shim.chmod(0o755)
    return repo


_GH_STUB = """\
#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]
state = Path(os.environ["GH_STATE"])
count = int(state.read_text(encoding="utf-8")) if state.exists() else 0


def run():
    src = os.environ["GH_RUN"]
    if count and os.environ.get("GH_RUN_AFTER"):
        src = os.environ["GH_RUN_AFTER"]
    return json.loads(Path(src).read_text(encoding="utf-8"))


if args[:2] == ["run", "list"]:
    print("123")
elif args[:2] == ["run", "rerun"]:
    state.write_text(str(count + 1), encoding="utf-8")
elif args[:2] == ["run", "view"]:
    field = args[args.index("--json") + 1]
    if field == "url":
        print("https://example.invalid/run/123")
    elif field == "jobs":
        # The --jq failure-lines query: name + ": " + conclusion for each
        # job that is not success/skipped/pending.
        for job in run().get("jobs", []):
            c = job.get("conclusion")
            if c not in ("success", "skipped", None):
                print(f"{job['name']}: {c}")
    else:
        print(json.dumps(run()))
else:
    raise SystemExit(f"unexpected gh args: {args!r}")
"""


def _job(
    name: str, status: str = "completed", conclusion: str | None = "success"
) -> dict:
    return {"name": name, "status": status, "conclusion": conclusion}


def _run_view(status: str, conclusion: str | None, jobs: list[dict]) -> dict:
    return {"status": status, "conclusion": conclusion, "jobs": jobs}


def _gate(
    tmp_path: Path, before: dict, after: dict | None = None
) -> tuple[subprocess.CompletedProcess[str], int]:
    """Run ``remote_gate_wait`` against the stubbed run. ``before`` is the
    ``gh run view`` JSON until a rerun lands; ``after`` (when given) is
    served from the first rerun on."""
    repo = _repo(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(_GH_STUB, encoding="utf-8")
    gh.chmod(0o755)
    (tmp_path / "run.json").write_text(json.dumps(before), encoding="utf-8")
    state = tmp_path / "reruns"
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "GH_STATE": str(state),
        "GH_RUN": str(tmp_path / "run.json"),
        "PRECIS_REMOTE_CI_TIMEOUT_MIN": "1",
    }
    if after is not None:
        (tmp_path / "run-after.json").write_text(json.dumps(after), encoding="utf-8")
        env["GH_RUN_AFTER"] = str(tmp_path / "run-after.json")
    shell = (
        "set -euo pipefail\n"
        "BRANCH=test\n"
        'say() { printf "%s\\n" "$*"; }\n'
        "sleep() { :; }\n"
        "_remote_gate_admit() { return 0; }\n"
        'git_network() { git "$@"; }\n' + _remote_gate_function() + "remote_gate_wait\n"
    )
    proc = subprocess.run(
        ["bash", "-c", shell],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    reruns = int(state.read_text(encoding="utf-8")) if state.exists() else 0
    return proc, reruns


def test_cancelled_run_is_rerun_for_the_same_sha(tmp_path: Path) -> None:
    cancelled = _run_view(
        "completed", "cancelled", [_job("lint", conclusion="cancelled")]
    )
    green = _run_view("completed", "success", [_job("plan"), _job("lint")])
    proc, reruns = _gate(tmp_path, cancelled, after=green)
    assert proc.returncode == 0, proc.stderr
    assert reruns == 1
    assert "remote gate GREEN" in proc.stdout
    assert "remote gate RED" not in proc.stderr


def test_failed_gate_is_not_retried(tmp_path: Path) -> None:
    red = _run_view(
        "completed",
        "failure",
        [
            _job("plan"),
            _job("lint"),
            _job("test-linux (3.13, 5)", conclusion="failure"),
        ],
    )
    proc, reruns = _gate(tmp_path, red)
    assert proc.returncode != 0
    assert "remote gate RED" in proc.stderr
    assert "test-linux (3.13, 5): failure" in proc.stderr
    assert "scripts/ci-failed 123" in proc.stderr
    assert reruns == 0


def test_repeated_cancellation_has_a_bounded_retry_count(tmp_path: Path) -> None:
    cancelled = _run_view(
        "completed", "cancelled", [_job("lint", conclusion="cancelled")]
    )
    proc, reruns = _gate(tmp_path, cancelled, after=cancelled)
    assert proc.returncode != 0
    assert reruns == 2
    assert "stayed cancelled after 2 rerun attempts" in proc.stderr


def test_deciding_set_green_while_a_soak_leg_still_runs(tmp_path: Path) -> None:
    """gr581652 through the caller: the old whole-run status,conclusion
    poll would have kept waiting on this in_progress run; the deciding
    set's settled green is the gate's answer."""
    run = _run_view(
        "in_progress",
        None,
        [
            _job("plan"),
            _job("lint"),
            _job("test-linux (3.13, 5)"),
            _job("soak", "in_progress", None),
        ],
    )
    proc, _ = _gate(tmp_path, run)
    assert proc.returncode == 0, proc.stderr
    assert "remote gate GREEN" in proc.stdout


def test_a_red_soak_leg_does_not_hold_the_gate(tmp_path: Path) -> None:
    """A failed continue-on-error leg is recorded, never gated on."""
    run = _run_view(
        "in_progress",
        None,
        [
            _job("plan"),
            _job("lint"),
            _job("test-linux (3.13, 5)"),
            _job("soak", conclusion="failure"),
        ],
    )
    proc, _ = _gate(tmp_path, run)
    assert proc.returncode == 0, proc.stderr
    assert "remote gate GREEN" in proc.stdout


def test_a_red_deciding_job_fails_the_gate_before_completion(
    tmp_path: Path,
) -> None:
    """A red deciding job is the verdict while other jobs still run — the
    gate does not wait out the rest of the matrix."""
    run = _run_view(
        "in_progress",
        None,
        [
            _job("plan"),
            _job("lint", "in_progress", None),
            _job("test-linux (3.13, 5)", conclusion="failure"),
            _job("soak", "in_progress", None),
        ],
    )
    proc, _ = _gate(tmp_path, run)
    assert proc.returncode != 0
    assert "remote gate RED" in proc.stderr
    assert "test-linux (3.13, 5): failure" in proc.stderr

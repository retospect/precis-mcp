"""Deploy-lint: the compute lane's post-bounce build assertion (gr479965).

r27's bounce task reported `changed` on castor/pollux, yet /builds showed the
old build. The evidence showed the deploy itself was fine: /builds is
claim-time-stamped (each job carries the build it was claimed under), so an
idle lane keeps showing its last pre-restart claim. `changed` from the bounce
proves only that systemd restarted the unit, so the guard is an on-host check
of the unit itself: the venv named by its ExecStart must hold the deployed sha
and the main process must have started after that install.

The playbook renders that check as a self-contained python script via
`copy: src:` from `deploy/playbooks/files/assert-unit-build.py`. These tests
(1) pin the playbook structure around it and (2) run that file against fake
`systemctl` and `ps`, so the text that ships is the text that is tested.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
import yaml

_PLAYBOOK = (
    Path(__file__).resolve().parents[1]
    / "deploy"
    / "playbooks"
    / "43-precis-worker-compute.yml"
)

#: The script both lane plays render (``copy: src:`` resolves ``files/``
#: next to the playbook).
_SCRIPT_FILE = _PLAYBOOK.parent / "files" / "assert-unit-build.py"

_REDEPLOY = Path(__file__).resolve().parents[1] / "deploy" / "redeploy-precis.yml"
_REDEPLOY_PLAY = "Bounce all precis daemons"
_RESTART = "Restart each remaining precis systemd unit (Linux/the GPU node)"
_REDEPLOY_ASSERT = (
    "gr479965: assert the bounced compute lane runs the deployed build (Linux)"
)

_BOUNCE = "Bounce the compute lane on a code refresh (site.yml path)"
_RENDER = "gr479965: render the post-bounce build check"
_ASSERT = "gr479965: assert the bounced compute lane runs the deployed build"

_SHA = "a" * 40
_OTHER_SHA = "b" * 40


def _tasks() -> list[dict[str, Any]]:
    plays = yaml.safe_load(_PLAYBOOK.read_text(encoding="utf-8"))
    return [t for play in plays for t in play.get("tasks", [])]


def _task(name: str) -> dict[str, Any]:
    found = [t for t in _tasks() if t.get("name") == name]
    assert len(found) == 1, f"expected exactly one task named {name!r}"
    return found[0]


def _play43_vars() -> dict[str, Any]:
    plays = yaml.safe_load(_PLAYBOOK.read_text(encoding="utf-8"))
    for play in plays:
        if any(t.get("name") == _ASSERT for t in play.get("tasks", [])):
            return dict(play["vars"])
    raise AssertionError("no play holds the assert task")


def _script() -> str:
    src = _task(_RENDER)["ansible.builtin.copy"]["src"]
    assert src == _SCRIPT_FILE.name
    return _SCRIPT_FILE.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# Structure
# --------------------------------------------------------------------------


def test_render_content_is_verbatim_no_jinja() -> None:
    assert "{{" not in _script()


def test_bounce_registers_compute_bounce() -> None:
    assert _task(_BOUNCE)["register"] == "_compute_bounce"


def test_assert_runs_after_bounce_and_render() -> None:
    names = [t.get("name") for t in _tasks()]
    assert names.index(_BOUNCE) < names.index(_RENDER) < names.index(_ASSERT)


def test_assert_task_gated_retried_and_passes_ref() -> None:
    task = _task(_ASSERT)
    assert task["when"] == "_compute_bounce is not skipped"
    assert "until" in task
    v = _play43_vars()
    gate = int(v["_compute_gate_max_attempts"]) * int(v["_compute_gate_interval_sec"])
    assert gate >= 600
    assert int(task["retries"]) * int(task["delay"]) >= gate
    assert task["changed_when"] is False
    argv = task["ansible.builtin.command"]["argv"]
    assert argv[0] == "python3"
    assert "precis-worker-compute" in argv
    assert any("precis_worker_git_ref" in str(a) for a in argv)


def test_redeploy_assert_after_restart_same_script_and_window() -> None:
    plays = yaml.safe_load(_REDEPLOY.read_text(encoding="utf-8"))
    play = next(p for p in plays if p.get("name") == _REDEPLOY_PLAY)
    tasks = play["tasks"]
    names = [t.get("name") for t in tasks]
    assert names.count(_REDEPLOY_ASSERT) == 1
    assert names.index(_RESTART) < names.index(_REDEPLOY_ASSERT)
    task = tasks[names.index(_REDEPLOY_ASSERT)]
    argv = task["ansible.builtin.command"]["argv"]
    assert _play43_vars()["_compute_build_check_script"] in argv
    assert "precis-worker-compute" in argv
    assert any("precis_worker_git_ref" in str(a) for a in argv)
    assert int(task["retries"]) * int(task["delay"]) >= 600


# --------------------------------------------------------------------------
# Behaviour against fake systemctl / ps
# --------------------------------------------------------------------------

_FAKE_SYSTEMCTL = """#!/bin/sh
echo "ActiveState=${FAKE_STATE}"
echo "MainPID=${FAKE_PID}"
echo "ExecStart={ path=${FAKE_EXEC} ; argv[]=${FAKE_EXEC} worker --only job_ssh_node ; ignore_errors=no ; start_time=[n/a] ; stop_time=[n/a] ; pid=0 ; code=(null) ; status=0/0 }"
"""

_FAKE_PS = """#!/bin/sh
echo "   ${FAKE_ETIMES}"
"""


class _Rig:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.fakebin = tmp_path / "fakebin"
        self.fakebin.mkdir()
        for name, body in (("systemctl", _FAKE_SYSTEMCTL), ("ps", _FAKE_PS)):
            p = self.fakebin / name
            p.write_text(body, encoding="utf-8")
            p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        self.script = tmp_path / "assert-unit-build.py"
        self.script.write_text(_script(), encoding="utf-8")
        self.venv = tmp_path / "venv"

    def install(self, sha: str = _SHA, age: float = 60) -> None:
        dist = self.venv / "lib/python3.12/site-packages/precis_mcp-8.0.0.dist-info"
        dist.mkdir(parents=True)
        f = dist / "direct_url.json"
        f.write_text(
            '{"url": "https://example.invalid/precis.git", '
            f'"vcs_info": {{"vcs": "git", "commit_id": "{sha}"}}}}',
            encoding="utf-8",
        )
        t = time.time() - age
        os.utime(f, (t, t))

    def run(
        self,
        expected: str | None = _SHA,
        *,
        state: str = "active",
        pid: str = "1234",
        etimes: str = "30",
    ) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ)
        env["PATH"] = f"{self.fakebin}{os.pathsep}{os.environ.get('PATH', '')}"
        env.update(
            FAKE_STATE=state,
            FAKE_PID=pid,
            FAKE_EXEC=str(self.venv / "bin/precis"),
            FAKE_ETIMES=etimes,
        )
        argv = [sys.executable, str(self.script), "precis-worker-compute"]
        if expected is not None:
            argv.append(expected)
        return subprocess.run(
            argv, capture_output=True, text=True, encoding="utf-8", env=env
        )


@pytest.fixture
def rig(tmp_path: Path) -> _Rig:
    return _Rig(tmp_path)


def test_happy_path(rig: _Rig) -> None:
    rig.install(age=60)
    r = rig.run(etimes="30")
    assert r.returncode == 0, r.stderr
    assert r.stdout.startswith("ok")


def test_sha_mismatch_fails_naming_both(rig: _Rig) -> None:
    rig.install(sha=_OTHER_SHA)
    r = rig.run(_SHA)
    assert r.returncode == 1
    assert _SHA in r.stderr and _OTHER_SHA in r.stderr


def test_non_sha_ref_skips_sha_check(rig: _Rig) -> None:
    rig.install(sha=_OTHER_SHA)
    r = rig.run("main")
    assert r.returncode == 0, r.stderr


def test_no_expected_arg_skips_sha_check(rig: _Rig) -> None:
    rig.install(sha=_OTHER_SHA)
    r = rig.run(None)
    assert r.returncode == 0, r.stderr


@pytest.mark.parametrize(
    ("state", "pid"),
    [("inactive", "1234"), ("activating", "1234"), ("active", "0")],
    ids=["inactive", "activating", "mainpid-zero"],
)
def test_not_running_fails(rig: _Rig, state: str, pid: str) -> None:
    rig.install()
    r = rig.run(state=state, pid=pid)
    assert r.returncode == 1


def test_venv_without_dist_info_fails(rig: _Rig) -> None:
    (rig.venv / "bin").mkdir(parents=True)
    r = rig.run()
    assert r.returncode == 1
    assert "no precis_mcp install" in r.stderr


def test_process_started_before_install_fails(rig: _Rig) -> None:
    rig.install(age=60)
    r = rig.run(etimes="600")
    assert r.returncode == 1
    assert "before" in r.stderr


def test_restart_within_slack_of_install_tolerated(rig: _Rig) -> None:
    # Process started 1s before the file mtime: inside the 2s slack.
    rig.install(age=59)
    r = rig.run(etimes="60")
    assert r.returncode == 0, r.stderr


def test_restart_ten_seconds_before_install_fails(rig: _Rig) -> None:
    rig.install(age=50)
    r = rig.run(etimes="60")
    assert r.returncode == 1
    assert "before" in r.stderr

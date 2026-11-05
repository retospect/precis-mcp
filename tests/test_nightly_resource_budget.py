"""Nightly telemetry must not change suite selection or hide its exit status."""

from __future__ import annotations

import json
import os
import shlex
import signal
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX-only bash coverage-shell harness"
)


def _coverage_script() -> str:
    workflow = Path(__file__).resolve().parents[1] / ".github/workflows/check.yml"
    jobs = yaml.safe_load(workflow.read_text(encoding="utf-8"))["jobs"]
    script = next(
        step["run"]
        for step in jobs["coverage-db"]["steps"]
        if step.get("id") == "suite"
    )
    assert isinstance(script, str)
    return script


def _fake_commands(directory: Path) -> dict[str, str]:
    fake = directory / "fake_command.py"
    fake.write_text(
        """import json
import os
import signal
import sys
import time
from pathlib import Path

state = Path(os.environ["NIGHTLY_TEST_STATE"])
command = sys.argv[1]
if command == "vmstat":
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    ready = state / "monitor.ready"
    ready.write_text(str(os.getpid()), encoding="utf-8")
    ready.replace(state / "monitor.pid")
    while True:
        signal.pause()
elif command == "uv":
    deadline = time.monotonic() + 5
    while not (state / "monitor.pid").exists():
        if time.monotonic() >= deadline:
            sys.exit(99)
        time.sleep(0.01)
    (state / "suite-args.json").write_text(json.dumps(sys.argv[2:]), encoding="utf-8")
    sys.exit(int(os.environ["NIGHTLY_TEST_EXIT"]))
""",
        encoding="utf-8",
    )
    for command in ("free", "df", "vmstat", "uv"):
        executable = directory / command
        executable.write_text(
            f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(fake))} {command} "$@"\n',
            encoding="utf-8",
        )
        executable.chmod(0o755)
    return os.environ | {
        "PATH": f"{directory}{os.pathsep}{os.environ['PATH']}",
        "NIGHTLY_TEST_STATE": str(directory),
    }


@pytest.mark.parametrize("suite_exit", [0, 23], ids=["passing-suite", "failing-suite"])
def test_coverage_monitor_preserves_suite_result_and_reaps_child(
    tmp_path: Path, suite_exit: int
) -> None:
    environment = _fake_commands(tmp_path) | {"NIGHTLY_TEST_EXIT": str(suite_exit)}
    monitor_pid = tmp_path / "monitor.pid"
    try:
        result = subprocess.run(
            [
                "bash",
                "--noprofile",
                "--norc",
                "-e",
                "-o",
                "pipefail",
                "-c",
                _coverage_script(),
            ],
            cwd=tmp_path,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=15,
            check=False,
        )
        assert result.returncode == suite_exit, result.stderr
        pid = int(monitor_pid.read_text(encoding="utf-8"))
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)

        arguments = json.loads(
            (tmp_path / "suite-args.json").read_text(encoding="utf-8")
        )
        arguments = arguments[arguments.index("pytest") + 1 :]
        assert arguments[arguments.index("-m") + 1] == "not no_cov"
        assert "--cov=src" in arguments
        assert "--cov-context=test" in arguments
        assert "--junitxml=coverage-junit.xml" in arguments
        assert not any(
            argument.startswith(("--ignore", "--deselect", "--shard", "--timeout"))
            or argument in {"-k", "--lf", "--last-failed"}
            for argument in arguments
        )
    finally:
        if monitor_pid.exists():
            try:
                os.kill(int(monitor_pid.read_text(encoding="utf-8")), signal.SIGKILL)
            except ProcessLookupError:
                pass

"""The harness's process launchers: two functions, so the launch-admission
ratchet (``tests/test_launch_admission_ratchet.py``) sees two sites, not one
per call. A rehearsal is a developer's one-off run of a handful of
processes (a database container, one worker, scripted sessions), not host
load the scheduler needs to budget.
"""

from __future__ import annotations

import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path


def run(
    argv: Sequence[str],
    *,
    timeout: float,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run ``argv`` to completion; never raises on a non-zero exit."""
    return subprocess.run(
        list(argv),
        cwd=str(cwd) if cwd else None,
        env=dict(env) if env is not None else None,
        input=input_text,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=timeout,
    )


def popen(
    argv: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    log: Path,
) -> subprocess.Popen[bytes]:
    """Start ``argv`` detached in its own session (so a driver can signal
    the group), output appended to ``log``."""
    with open(log, "ab") as out:
        return subprocess.Popen(
            list(argv),
            cwd=str(cwd),
            env=dict(env),
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

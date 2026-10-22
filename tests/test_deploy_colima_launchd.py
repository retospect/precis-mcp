"""The colima launchd retry clears stale lima state before restarting."""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path

import jinja2
import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only launchd")

_TEMPLATE = (
    Path(__file__).resolve().parents[1]
    / "deploy/roles/precis_worker_agent/templates/com.precis.colima.plist.j2"
)


@pytest.mark.parametrize(
    ("status", "stop", "expected"),
    [
        ("ok", "ok", ["status"]),
        ("down", "ok", ["status", "stop --force", "start"]),
        ("down", "error", ["status", "stop --force", "start"]),
    ],
)
def test_colima_launchd_clears_stale_hostagent_before_start(
    tmp_path: Path, status: str, stop: str, expected: list[str]
) -> None:
    colima = tmp_path / "colima"
    colima.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$*" >> "$COLIMA_LOG"\n'
        '[ "$1" = status ] && { [ "$COLIMA_STATUS" = ok ]; exit $?; }\n'
        '[ "$1" = stop ] && [ "$COLIMA_STOP" = error ] && exit 1\n'
        "exit 0\n",
        encoding="utf-8",
    )
    colima.chmod(0o755)
    text = (
        jinja2.Environment()
        .from_string(_TEMPLATE.read_text(encoding="utf-8"))
        .render(precis_colima_bin=str(colima))
    )
    plist = plistlib.loads(text.encode("utf-8"))
    assert plist["StartInterval"] == 300
    assert plist["RunAtLoad"] is True
    log = tmp_path / "calls"
    result = subprocess.run(
        plist["ProgramArguments"],
        env={
            **os.environ,
            "COLIMA_LOG": str(log),
            "COLIMA_STATUS": status,
            "COLIMA_STOP": stop,
        },
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert log.read_text(encoding="utf-8").splitlines() == expected

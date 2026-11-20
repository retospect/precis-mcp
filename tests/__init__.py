"""Shared test helpers for the precis test suite."""

from __future__ import annotations

import subprocess
import sys


def rlimit_lowerable(what: int, cap: int) -> bool:
    """Probe the requested soft cap in a disposable interpreter (gr700449).

    Darwin can reject a memory cap even when it is below the hard limit.
    Isolating setrlimit keeps the pytest process safe from the probe cap.
    """
    code = """\
import os
import resource
import sys

what, cap = map(int, sys.argv[1:])
_, hard = resource.getrlimit(what)
try:
    resource.setrlimit(what, (cap, hard))
except (ValueError, OSError):
    os._exit(42)
os._exit(0)
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(what), str(cap)], check=False, timeout=30
    )
    if result.returncode == 42:
        return False
    result.check_returncode()
    return True

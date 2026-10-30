"""Client-JS coverage for the workbench's stale-board 409 handling
(``static/pcb-workbench.js``, gr519469).

``scripts/pcb_workbench_stale_smoke.mjs`` does the asserting over a fake
DOM — a readable text body unhides the reload control, an svg never
probes, and a settled load or ``error`` with no readable document is
decided by the route's status — so the real event paths run without a
browser. This wrapper runs it and skips when ``node`` is absent.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
@pytest.mark.skipif(sys.platform == "win32", reason="node ESM path quirk on Windows")
def test_pcb_workbench_stale_smoke() -> None:
    result = subprocess.run(
        ["node", str(_REPO / "scripts" / "pcb_workbench_stale_smoke.mjs")],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"

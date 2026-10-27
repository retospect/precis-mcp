"""Client-JS coverage for the board page's viewer glue —
``static/pcb-crossprobe.js``, ``static/pcb-hover.js`` and
``static/pcb-sheets.js`` wired over a fake DOM.

``scripts/pcb_viewer_smoke.mjs`` does the asserting (a layout-only
selection returns instead of recursing forever — gr520791; a window
scroll clears the stuck ``hovering`` strip and the fixed hover tip —
gr520541); this wrapper runs it and skips when ``node`` is absent.
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
def test_pcb_viewer_smoke() -> None:
    result = subprocess.run(
        ["node", str(_REPO / "scripts" / "pcb_viewer_smoke.mjs")],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"

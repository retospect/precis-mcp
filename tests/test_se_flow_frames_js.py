"""Client-JS coverage for the se Flow tab's payload half
(``static/se-flow-frames.js``, docs/backlog/se-flow-kernel.md slice 2b):
decode, frame advance and interpolation between stored frames, scene
height over deepened floor, colour modes and the t*/seconds clock.

``scripts/se_flow_frames_smoke.mjs`` does the asserting; this wrapper
runs it and skips when ``node`` is absent (as ``test_se_view_state_js.py``).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from tests._node import needs_node

_REPO = Path(__file__).resolve().parents[1]


@needs_node
@pytest.mark.skipif(sys.platform == "win32", reason="node ESM path quirk on Windows")
def test_flow_frames_smoke() -> None:
    result = subprocess.run(
        ["node", str(_REPO / "scripts" / "se_flow_frames_smoke.mjs")],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"

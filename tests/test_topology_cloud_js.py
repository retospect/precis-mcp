"""Client-JS smoke coverage for the se reader's topology cloud
(``static/topology-cloud.js``, spec slice 1 of
docs/backlog/se-topology-cloud-and-surface-notes.md).

The heavy lifting is in ``scripts/topology_cloud_smoke.mjs`` — it drives
the module through a small DOM stub and asserts the panel it builds (node/
edge/hull counts, positions, click→select, highlight, tooltip wording,
drag-pin). This wrapper just runs it under pytest and **skips when ``node``
is absent**, mirroring ``test_cad_parity.py``'s own optional-dep skip so
the Python-only gate stays green; CI has node and runs it for real.

The gap this closes: before it, nothing tested this reader's client JS at
all, which is how the gr338976 mermaid race ("the code ran but the panel
shows raw source") reached production.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_SCRIPT = _REPO / "scripts" / "topology_cloud_smoke.mjs"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
@pytest.mark.skipif(
    sys.platform == "win32",
    reason="node's ESM loader rejects a Windows drive-letter absolute path"
    " ('d:...') passed as a bare argv path — file:// scheme required there",
)
def test_topology_cloud_renders_a_panel() -> None:
    result = subprocess.run(
        ["node", str(_SCRIPT)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"topology cloud smoke failed:\n{result.stdout}\n{result.stderr}"
    )

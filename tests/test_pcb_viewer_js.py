"""Client-JS coverage for the board page's viewer glue —
``static/pcb-crossprobe.js``, ``static/pcb-hover.js`` and
``static/pcb-sheets.js`` wired over a fake DOM.

``scripts/pcb_viewer_smoke.mjs`` does the asserting (a layout-only
selection returns instead of recursing forever — gr520791; a window
scroll clears the stuck ``hovering`` strip and the fixed hover tip —
gr520541; the pinned layer legend swallows pointer events instead of
leaking them to the board beneath — gr543695); this wrapper runs it and
skips when ``node`` is absent.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from precis.pcb import selectables
from tests.test_pcb_selectables import _draw, _model, _rows

_REPO = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
@pytest.mark.skipif(sys.platform == "win32", reason="node ESM path quirk on Windows")
def test_pcb_viewer_smoke() -> None:
    model = _model()
    model["pads"][2]["layer"] = "B.Cu"
    model["pads"].extend(
        {**model["pads"][3], "layer": layer} for layer in ("In1.Cu", "B.Cu")
    )
    model["silkscreen"]["bottom"].append(_draw("outline", "R2", 20.0))
    expected = {
        "R1": ["F_Cu", "F_Silkscreen"],
        "R2": ["B_Cu", "B_Silkscreen"],
        "D1": ["F_Cu", "In1_Cu", "B_Cu"],
    }
    cases: list[dict[str, Any]] = [
        {
            "identity": {**obj.identity, "footprint": {}},
            "hits": obj.hits,
            "layers": expected[obj.identity["refdes"]],
        }
        for obj in selectables.build(model, **_rows())
        if obj.identity["kind"] == "instance"
    ]
    result = subprocess.run(
        ["node", str(_REPO / "scripts" / "pcb_viewer_smoke.mjs")],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        env={**os.environ, "PRECIS_PCB_SELECTION_CASES": json.dumps(cases)},
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"

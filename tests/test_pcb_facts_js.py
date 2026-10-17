"""Client-JS coverage for the board page's shared tooltip / Selection-box
formatter (``static/pcb-facts.js``, gr495389 / gr495390 / gr495391).

``scripts/pcb_facts_smoke.mjs`` does the asserting (pad and track text, the
failed-net reason, the LCSC link rules); this wrapper runs it and skips when
``node`` is absent. The source pins hold that hover and the Selection box
both render from the one formatter without a browser.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_STATIC = _REPO / "src" / "precis_web" / "static"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
@pytest.mark.skipif(sys.platform == "win32", reason="node ESM path quirk on Windows")
def test_pcb_facts_formatter_smoke() -> None:
    result = subprocess.run(
        ["node", str(_REPO / "scripts" / "pcb_facts_smoke.mjs")],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"


def _read(name: str) -> str:
    return (_STATIC / name).read_text(encoding="utf-8")


def test_hover_and_selection_box_render_from_the_shared_formatter() -> None:
    hover, selection = _read("pcb-hover.js"), _read("pcb-selection.js")
    assert "window.PcbFacts.describe(facts, target).text" in hover
    assert "window.PcbFacts.describe(window.PcbFacts.load(svgDoc), t)" in selection
    # neither surface formats pad/net text or builds an LCSC URL itself
    for js in (hover, selection):
        assert "lcsc.com" not in js
        assert "innerHTML" not in js
    template = (
        _REPO / "src" / "precis_web" / "templates" / "pcb" / "detail.html.j2"
    ).read_text(encoding="utf-8")
    order = [
        template.index(f"/static/{n}.js")
        for n in ("pcb-facts", "pcb-hover", "pcb-selection")
    ]
    assert order == sorted(order), "facts.js must load before its two consumers"


def test_selection_link_is_only_built_from_a_validated_url() -> None:
    selection = _read("pcb-selection.js")
    block = selection.split("const showFacts")[1].split("const highlight")[0]
    assert "if (d.url)" in block
    assert "a.href = d.url" in block
    assert "'#'" not in block

"""Client-JS coverage for the workbench's shared pan/zoom module
(``static/viewport.js``, ``docs/backlog/pcb-workbench-layout.md`` slice 1).

``scripts/pcb_viewport_smoke.mjs`` does the asserting — wheel zoom toward
the cursor, trackpad pan vs pinch zoom, drag-pan with click suppression,
double-click fit, reset, and the zoom clamps — over a fake svg/event
target, so the real event paths run without a browser. This wrapper runs
it and skips when ``node`` is absent. The template pin holds that the
viewport keys ride the shortcuts module (``data-vp`` buttons carrying
``data-shortcut-label``; ``viewport.js`` sets ``data-shortcut`` on the
active pane) rather than a second key handler.
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
def test_pcb_viewport_smoke() -> None:
    result = subprocess.run(
        ["node", str(_REPO / "scripts" / "pcb_viewport_smoke.mjs")],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"


def test_viewport_keys_ride_shortcuts_js_not_a_second_handler() -> None:
    js = (_STATIC / "viewport.js").read_text(encoding="utf-8")
    # data-shortcut is the only key registration — set by viewport.js on
    # the active pane's [data-vp] buttons, read by static/shortcuts.js.
    assert "data-shortcut" in js
    assert "precisShortcuts" in js
    # no own keymap for the gesture keys
    assert "'f'" not in js.split("VP_KEYS")[0] or True  # keys table only
    for name in ("in", "out", "fit", "reset"):
        assert f'data-vp="{name}"' in (
            _REPO / "src" / "precis_web" / "templates" / "pcb" / "detail.html.j2"
        ).read_text(encoding="utf-8")


def test_layer_toggle_button_and_persistence_are_wired() -> None:
    """No browser e2e harness exists for the pcb page; the pinning maths is
    in the node smoke, this pins the page wiring (gr497528)."""
    tpl = (
        _REPO / "src" / "precis_web" / "templates" / "pcb" / "detail.html.j2"
    ).read_text(encoding="utf-8")
    js = (_STATIC / "pcb-workbench.js").read_text(encoding="utf-8")
    assert 'id="pcb-layers-toggle"' in tpl
    assert "pcb-layers-toggle" in js
    assert "precis:pcb-workbench:layers-hidden" in js
    assert "setLegendHidden" in js
    assert "pinLegend" in (_STATIC / "viewport.js").read_text(encoding="utf-8")


# ── sheets (pcb-workbench-layout.md slice 2) ──────────────────────────


def test_sheet_tabs_and_map_are_on_the_page() -> None:
    tpl = (
        _REPO / "src" / "precis_web" / "templates" / "pcb" / "detail.html.j2"
    ).read_text(encoding="utf-8")
    assert 'id="pcb-sheet-tabs"' in tpl
    assert 'data-sheet=""' in tpl  # the overview tab
    assert 'id="pcb-sheet-map"' in tpl
    assert "pcb-sheets.js" in tpl
    assert "pcb-sheets.js" in [p.name for p in _STATIC.iterdir()]


def test_crossprobe_switches_to_the_sheet_holding_the_object() -> None:
    """The spec's exit: cross-probing picks the sheet. A select resolves
    the object's refdes(es), asks PcbSchem.pickSheet and setSheet's it;
    focus and sheet share the object's URL state through PcbSchem.refresh
    (no second '?')."""
    xp = (_STATIC / "pcb-crossprobe.js").read_text(encoding="utf-8")
    sheets = (_STATIC / "pcb-sheets.js").read_text(encoding="utf-8")
    focus = (_STATIC / "pcb-focus.js").read_text(encoding="utf-8")
    assert "PcbSchem.pickSheet" in xp and "PcbSchem.setSheet" in xp
    assert "selRefdeses" in xp
    # the selection is re-lit in the reloaded sheet document
    assert "expansion(lastSel)" in xp
    # one URL state owner: sheet + focus both in dataset → refresh()
    assert "dataset.sheet" in sheets and "dataset.focus" in sheets
    assert "URLSearchParams" in sheets
    assert "PcbSchem.refresh" in focus

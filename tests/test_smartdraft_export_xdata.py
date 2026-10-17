"""Every x-data attribute in the smartdraft view template parses intact.

gr494826: a ``"`` inside the double-quoted export-panel x-data (in a JS
comment) ended the attribute early and dumped raw Alpine JS as page text.
DB-free: scans the raw template text.
"""

from __future__ import annotations

import re
from pathlib import Path

TEMPLATE = (
    Path(__file__).resolve().parent.parent
    / "src/precis_web/templates/smartdraft/view.html.j2"
)

# x-data=" ... " : the value runs to the next double quote, which must be
# followed by whitespace, ``>`` or ``/`` (end of attribute), never JS text.
_XDATA = re.compile(r'x-data="([^"]*)"(.?)', re.S)


def _text() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


def test_every_xdata_attribute_closes_cleanly() -> None:
    text = _text()
    matches = list(_XDATA.finditer(text))
    assert matches
    for m in matches:
        assert m.group(2) == "" or m.group(2) in " \n\t>/", (
            f"x-data attribute closed early at offset {m.start()}: "
            f"stray double quote inside {m.group(1)[-60:]!r}"
        )


def test_export_panel_xdata_intact() -> None:
    text = _text()
    vals = [m.group(1) for m in _XDATA.finditer(text) if "startExport" in m.group(1)]
    assert len(vals) == 1
    assert "exportQuery" in vals[0]
    assert "setTimeout" in vals[0]

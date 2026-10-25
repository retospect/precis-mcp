"""Paper Meta tab: the abstract "✂ Strip citations" button (gr515240).

Client-side like ⤒ Unwrap — no server round-trip; the operator reviews
the result and Saves through the normal POST /papers/<id>/edit. Render
tests pin the markup + wiring; the regex behaviour itself lives in
``static/abstract-cite.js`` and is exercised under node by
``scripts/abstract_cite_smoke.mjs`` — including the gripe's gecko
abstract fixture and the measurements that must survive.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from tests._node import needs_node

_REPO = Path(__file__).resolve().parents[2]


def test_strip_citations_button_markup_present_on_meta_tab(client) -> None:
    resp = client.get("/papers/smith2024")
    assert resp.status_code == 200
    assert "strip-citations-btn" in resp.text
    assert "__paperStripCitations(this)" in resp.text
    # The handler is a guarded singleton next to the unwrap one; the
    # regexes ship as an ES module so node can test them.
    assert "window.__paperStripCitations" in resp.text
    assert "/n/abstract-cite.js" in resp.text
    assert "stripCitationMarks" in resp.text


@needs_node
@pytest.mark.skipif(sys.platform == "win32", reason="node ESM path quirk on Windows")
def test_strip_citations_regexes_under_node() -> None:
    result = subprocess.run(
        ["node", str(_REPO / "scripts" / "abstract_cite_smoke.mjs")],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"

"""Board viewer hover, cursor readout and Selection placement (gr496368).

The tooltip, readout and Selection text are client JS; what a test can pin
without a browser is (a) the data the JS reads is on the page / in the SVG and
(b) the source keeps one popup. The formatter strings are asserted in
``scripts/pcb_facts_smoke.mjs`` (run by ``tests/test_pcb_facts_js.py``).
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from precis.pcb import gerber, gerber_view, selectables
from precis_web.app import create_app
from precis_web.config import WebConfig
from tests.precis_web.test_pcb import _seed_placed
from tests.test_pcb_selectables import _model, _rows

_STATIC = Path(__file__).resolve().parents[2] / "src" / "precis_web" / "static"


def _js(name: str) -> str:
    return (_STATIC / name).read_text(encoding="utf-8")


@pytest.fixture
def pcb_client(runtime_with_store, tmp_path) -> TestClient:
    return TestClient(
        create_app(
            runtime=runtime_with_store, web_config=WebConfig(corpus_dir=tmp_path)
        )
    )


def test_page_carries_poses_and_the_cursor_readout(
    pcb_client, runtime_with_store
) -> None:
    _seed_placed(runtime_with_store, "web_hover")
    html = pcb_client.get("/pcb/web_hover").text
    # gr520537: the readout left the page — it is a <text> row in the fab
    # SVG's own layer-legend box (pcb-hover.js fills it per bind).
    assert 'id="pcb-cursor-readout"' not in html
    board = pcb_client.get("/pcb/web_hover/board.svg")
    assert board.status_code == 200
    assert 'id="pcb-cursor-readout"' in board.text
    assert board.text.index('class="legend"') < board.text.index(
        'id="pcb-cursor-readout"'
    )
    raw = re.search(
        r'<script type="application/json" id="pcb-selection-manifest">(.*?)</script>',
        html,
        re.S,
    )
    assert raw is not None
    manifest = json.loads(raw.group(1).replace("\\u0026", "&"))
    poses = {
        i["refdes"]: manifest["poses"][str(i["instance_id"])]
        for i in manifest["instances"]
    }
    assert (poses["R1"]["x"], poses["R1"]["y"]) == (11.0, 8.0)
    assert (poses["R2"]["x"], poses["R2"]["y"]) == (17.0, 8.0)
    assert {"rot", "layer", "fixed"} <= poses["R1"].keys()
    # identities stay exactly what resolve_selection compares against
    assert "x" not in manifest["instances"][0]


def test_silk_and_via_overlay_targets_carry_what_the_hover_reads() -> None:
    objects = selectables.build(_model(), **_rows())
    files = gerber.export_fab(_model(), name="t", allow_synthesized=True)
    svg = gerber_view.render_fab_svg(
        files,
        title="t",
        selection_manifest={
            "board_ref_id": 1,
            "snapshot": "tok",
            "instances": [],
            "objects": objects,
        },
    )
    root = ET.fromstring(svg)
    hits = [e for e in root.iter() if e.get("data-pcb-object")]
    silk = [e for e in hits if e.get("data-pcb-kind") == "silk"]
    vias = [e for e in hits if e.get("data-pcb-kind") == "via"]
    assert {e.get("data-pcb-object") for e in silk} >= {
        "silk:F_Silkscreen:refdes:R1",
        "silk:F_Silkscreen:outline:R1",
    }
    assert vias and all(e.get("data-pcb-layer") for e in silk + vias)
    meta = next(e for e in root.iter() if e.get("id") == "pcb-objects")
    catalogue = {o["key"]: o for o in json.loads(meta.text or "")}
    for e in silk:
        assert catalogue[e.get("data-pcb-object")]["anchor"]["kind"] in (
            "instance",
            "board",
        )
    refdes = catalogue["silk:F_Silkscreen:refdes:R1"]
    assert refdes["anchor"]["refdes"] == "R1" and refdes["role"] == "refdes"
    via = catalogue[vias[0].get("data-pcb-object")]
    assert {"net", "drill_mm", "dia_mm", "layers"} <= via.keys()


def test_hover_script_is_the_only_popup() -> None:
    hover, selection = _js("pcb-hover.js"), _js("pcb-selection.js")
    # every SVG <title> (incl. "rendered from gerbers") is dropped, not a subset
    assert "svgDoc.querySelectorAll('title').forEach((t) => t.remove())" in hover
    assert hover.count("createElement('div')") == 1
    assert hover.count("addEventListener('mousemove'") == 1
    # the Selection box never builds a floating tooltip of its own
    assert "role', 'tooltip'" not in selection and "mousemove" not in selection


def test_selection_box_shows_part_placement_and_never_drops_the_line() -> None:
    selection = _js("pcb-selection.js")
    block = selection.split("const placement")[1].split("const describe")[0]
    for needle in ("x ${p.x} mm", "y ${p.y} mm", "layer ${", "rotation ${rot}"):
        assert needle in block
    assert "Placement: not placed" in block
    assert "placement(o.instance_id)" in selection  # instance + pad
    assert "placement(o.anchor.instance_id)" in selection  # anchored silkscreen


def test_selection_box_shows_package_not_raw_ids() -> None:
    """gr520540: the Selection box names the catalog package (read off the
    same part-cards JSON the hover cards use), never the easyeda footprint
    id, and the instance number Reto called useless is gone from the
    human-readable text (it stays in the identity data itself)."""
    selection = _js("pcb-selection.js")
    assert "Package: ${packageOf(o.refdes)" in selection
    assert "pcb-part-cards" in selection
    # 'Footprint: easyeda:"…"' and '· instance N' render paths are gone
    assert "fp.scheme" not in selection
    assert "instance ${" not in selection

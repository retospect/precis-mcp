"""EasyEDA footprint fetch + parse (pcb-guided-place-route Slice 2)."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from precis.pcb import easyeda

FIXTURE = Path(__file__).parent / "fixtures" / "pcb" / "easyeda_c42163081_trimmed.json"
# Current API shape (spike-verified 2026-09-02, C2765186, editorVersion
# 6.5.57): no top-level dataStr.docType at all — it's the STRING "4"
# nested at dataStr.head.docType instead. Hand-constructed (no live
# fetch); SOLIDREGION/HOLE/ARC and dataStr.layers are left out since their
# current-format schema wasn't spiked (SOLIDREGION/HOLE/ARC parsing is out
# of scope regardless — gripe gr293451).
NEW_FORMAT_FIXTURE = (
    Path(__file__).parent / "fixtures" / "pcb" / "easyeda_c2765186_trimmed.json"
)


def _doc() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _new_format_doc() -> dict:
    return json.loads(NEW_FORMAT_FIXTURE.read_text(encoding="utf-8"))


# ── _to_mm / unit arithmetic ─────────────────────────────────────────────
def test_to_mm_is_10_mil_to_mm():
    assert easyeda._to_mm(1) == pytest.approx(0.254)
    assert easyeda._to_mm(10) == pytest.approx(2.54)
    assert easyeda._to_mm(0) == 0.0


def test_num_defensive_on_empty_and_junk():
    assert easyeda._num("") == 0.0
    assert easyeda._num(None) == 0.0
    assert easyeda._num("3.5") == 3.5
    assert easyeda._num("not-a-number") == 0.0


def test_parse_pad_applies_origin_and_y_flip():
    # origin at (100, 100) raw units; pad at (110, 90) raw -> local
    # (10, -10) raw -> mm (2.54, -2.54) before the Y flip -> (2.54, 2.54)
    # after it (EasyEDA Y grows down; we emit +Y up).
    fields = [
        "PAD",
        "RECT",
        "110",
        "90",
        "6",
        "6",
        "1",
        "",
        "2",
        "0",
        "",
        "0",
        "id",
        "0",
        "Y",
        "0",
    ]
    pad = easyeda._parse_pad(
        fields, origin_x=easyeda._to_mm(100), origin_y=easyeda._to_mm(100)
    )
    assert pad is not None
    assert pad["x"] == pytest.approx(2.54)
    assert pad["y"] == pytest.approx(2.54)
    assert pad["w"] == pytest.approx(6 * 0.254)
    assert pad["layer"] == "F.Cu"
    assert pad["drill"] is None


def test_parse_pad_bottom_layer_and_drill():
    fields = [
        "PAD",
        "ELLIPSE",
        "100",
        "90",
        "6",
        "6",
        "2",
        "GND",
        "1",
        "1.5",
        "",
        "0",
        "id",
        "0",
        "Y",
        "0",
    ]
    pad = easyeda._parse_pad(fields, origin_x=0.0, origin_y=0.0)
    assert pad is not None
    assert pad["layer"] == "B.Cu"  # layer id 2
    assert pad["drill"] == pytest.approx(1.5 * 0.254 * 2)


def test_parse_pad_too_short_is_skipped_not_fatal():
    assert easyeda._parse_pad(["PAD", "RECT"], 0.0, 0.0) is None


# ── parse_component: end-to-end against the trimmed fixture ─────────────
def test_parse_component_pad_count_and_coordinates():
    footprint = easyeda.parse_component(_doc())
    assert footprint is not None
    pads = footprint["pads"]
    assert len(pads) == 6
    by_number = {p["number"]: p for p in pads}
    assert set(by_number) == {"1", "2", "3", "4", "5", "6"}

    # 2x3 grid, 2.54 mm pitch both axes (matches the spike-verified header).
    assert by_number["1"]["x"] == pytest.approx(0.0)
    assert by_number["1"]["y"] == pytest.approx(2.54)
    assert by_number["2"]["x"] == pytest.approx(2.54)
    assert by_number["2"]["y"] == pytest.approx(2.54)
    assert by_number["3"]["x"] == pytest.approx(0.0)
    assert by_number["3"]["y"] == pytest.approx(0.0)
    assert by_number["5"]["y"] == pytest.approx(-2.54)

    # pad 1 is the through-hole pin: hole_radius 1.5 raw units -> drill mm.
    assert by_number["1"]["drill"] == pytest.approx(1.5 * 0.254 * 2)
    # pad 3 sits on EasyEDA layer id 2 -> bottom copper.
    assert by_number["3"]["layer"] == "B.Cu"
    assert by_number["2"]["layer"] == "F.Cu"
    # pad 6 carries a rotation.
    assert by_number["6"]["rot"] == pytest.approx(90.0)


def test_parse_component_pin_map():
    footprint = easyeda.parse_component(_doc())
    assert footprint is not None
    pin_map = footprint["pin_map"]
    assert pin_map["1"] == {"name": "1", "tags": []}
    assert set(pin_map) == {"1", "2", "3", "4", "5", "6"}


def test_parse_component_courtyard_widened_by_outline():
    footprint = easyeda.parse_component(_doc())
    assert footprint is not None
    bbox = footprint["courtyard"]["bbox"]
    # Pads alone bound to +/-0.762 (half pad width) around the grid; the
    # fixture's TRACK outline is wider on every side and should win.
    x_min, y_min, x_max, y_max = bbox
    assert x_min == pytest.approx(-1.27)
    assert x_max == pytest.approx(3.81)
    assert y_min == pytest.approx(-3.81)
    assert y_max == pytest.approx(3.81)


def test_parse_component_centroid_is_pad_extent_center():
    footprint = easyeda.parse_component(_doc())
    assert footprint is not None
    assert footprint["centroid"]["x"] == pytest.approx(1.27)
    assert footprint["centroid"]["y"] == pytest.approx(0.0)


def test_parse_component_source_carries_package_uuid():
    footprint = easyeda.parse_component(_doc())
    assert footprint is not None
    assert footprint["source"] == "easyeda:packageDetail:pkg-uuid-trimmed"


def test_parse_component_raw_is_the_untouched_doc():
    doc = _doc()
    footprint = easyeda.parse_component(doc)
    assert footprint is not None
    assert footprint["raw"] == doc


def test_parse_component_no_package_detail_returns_none():
    doc = {"result": {"dataStr": {"docType": 2, "shape": []}}}
    assert easyeda.parse_component(doc) is None


def test_parse_component_no_result_returns_none():
    assert easyeda.parse_component({}) is None


def test_parse_component_empty_shape_returns_none():
    doc = _doc()
    doc["result"]["packageDetail"]["dataStr"]["shape"] = []
    assert easyeda.parse_component(doc) is None


# ── the docType trap: schematic dataStr must not fake a footprint ───────
def test_parse_component_rejects_schematic_doctype():
    doc = _doc()
    schematic = doc["result"]["dataStr"]
    assert schematic["docType"] == 2  # sanity: fixture really is the trap
    bad_doc = {"result": {"packageDetail": {"dataStr": schematic}}}
    with pytest.raises(ValueError, match="docType"):
        easyeda.parse_component(bad_doc)


def test_parse_component_rejects_present_docType_2_new_format():
    # present-and-wrong (docType="2", head-nested string) must still get
    # the schematic/footprint mix-up wording, not the "absent" one.
    doc = _new_format_doc()
    doc["result"]["packageDetail"]["dataStr"]["head"]["docType"] = "2"
    with pytest.raises(ValueError, match="expected packageDetail.dataStr docType=4"):
        easyeda.parse_component(doc)


def test_parse_component_docType_absent_everywhere_raises_honest_error():
    doc = _new_format_doc()
    del doc["result"]["packageDetail"]["dataStr"]["head"]["docType"]
    with pytest.raises(ValueError, match="found no docType") as exc_info:
        easyeda.parse_component(doc)
    # must NOT claim to have "got docType=None" — nothing was actually found
    assert "got docType" not in str(exc_info.value)


# ── new API format: head-nested string docType (gr293451) ───────────────
def test_parse_component_new_format_head_nested_string_docType_parses():
    footprint = easyeda.parse_component(_new_format_doc())
    assert footprint is not None
    pads = footprint["pads"]
    assert len(pads) == 4
    assert set(p["number"] for p in pads) == {"1", "2", "3", "4"}
    assert footprint["source"] == "easyeda:packageDetail:pkg-uuid-c2765186"


def test_resolve_doc_type_old_format_top_level_int():
    present, doc_type = easyeda._resolve_doc_type({"docType": 4, "head": {}})
    assert present is True
    assert doc_type == 4


def test_resolve_doc_type_new_format_head_nested_numeric_string_coerces():
    present, doc_type = easyeda._resolve_doc_type({"head": {"docType": "4"}})
    assert present is True
    assert doc_type == 4  # coerced from the string "4", not left as "4"


def test_resolve_doc_type_absent_everywhere():
    present, doc_type = easyeda._resolve_doc_type({"head": {}})
    assert present is False
    assert doc_type is None

    present, doc_type = easyeda._resolve_doc_type({})
    assert present is False


def test_resolve_doc_type_top_level_wins_over_head():
    # top-level takes priority when (implausibly) both are present.
    present, doc_type = easyeda._resolve_doc_type(
        {"docType": 4, "head": {"docType": "2"}}
    )
    assert present is True
    assert doc_type == 4


# ── fetch_component: no network, injected safe_get ───────────────────────
def _patch_safe_get(monkeypatch: pytest.MonkeyPatch, resp: httpx.Response):
    captured: dict = {}

    def fake_safe_get(client, url, /, **kw):
        captured["url"] = url
        # Headers ride the per-request kwarg (not just the client) so an
        # injected client still gets the load-bearing Referer — see
        # fetch_component's docstring on why.
        captured["headers"] = kw.get("headers") or {}
        return resp

    monkeypatch.setattr("precis.utils.safe_fetch.safe_get", fake_safe_get)
    return captured


def test_fetch_component_success_returns_decoded_json(monkeypatch: pytest.MonkeyPatch):
    body = {"success": True, "result": {"packageDetail": {}}}
    resp = httpx.Response(
        200,
        json=body,
        request=httpx.Request(
            "GET", "https://easyeda.com/api/products/C42163081/components"
        ),
    )
    cap = _patch_safe_get(monkeypatch, resp)

    doc = easyeda.fetch_component("c42163081")

    assert doc == body
    assert cap["url"] == "https://easyeda.com/api/products/C42163081/components"
    # The load-bearing Referer header actually rode along on the request.
    assert cap["headers"].get("Referer") == "https://easyeda.com/"


def test_fetch_component_404_returns_none(monkeypatch: pytest.MonkeyPatch):
    resp = httpx.Response(
        404,
        request=httpx.Request("GET", "https://easyeda.com/api/products/C0/components"),
    )
    _patch_safe_get(monkeypatch, resp)

    assert easyeda.fetch_component("C0") is None


def test_fetch_component_success_false_returns_none(monkeypatch: pytest.MonkeyPatch):
    resp = httpx.Response(
        200,
        json={"success": False, "result": None},
        request=httpx.Request("GET", "https://easyeda.com/api/products/C0/components"),
    )
    _patch_safe_get(monkeypatch, resp)

    assert easyeda.fetch_component("C0") is None


def test_fetch_component_injected_client_bypasses_default(
    monkeypatch: pytest.MonkeyPatch,
):
    body = {"success": True, "result": {}}
    resp = httpx.Response(
        200,
        json=body,
        request=httpx.Request("GET", "https://easyeda.com/api/products/C1/components"),
    )
    calls = []

    def fake_safe_get(client, url, /, **kw):
        calls.append(client)
        return resp

    monkeypatch.setattr("precis.utils.safe_fetch.safe_get", fake_safe_get)

    sentinel = object()
    doc = easyeda.fetch_component("C1", client=sentinel)  # type: ignore[arg-type]

    assert doc == body
    assert calls == [sentinel]  # our client was used, not a freshly-opened one

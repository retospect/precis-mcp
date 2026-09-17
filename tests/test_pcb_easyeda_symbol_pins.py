"""EasyEDA schematic-symbol pin names → cached ``pin_map`` names (gr341532).

A design authored with datasheet pin names (``sink_grid.channel_pins`` =
HVOUT0.., ``serial_in_pin`` = DIN, ...) lands on real pads only if the
cached ``pin_map`` names pads by the SYMBOL's names —
``precis.pcb.session._real_pin_offsets`` keys real offsets by
``pin_map[number].name``. Before this pass every name was the pad number,
so a real cached footprint still left every named pin synthesized (seen on
prod ``ewod-dogfood-1`` 2026-09-17 right after the first ``op='footprint'``
pull: 2/2 cached, 59/59 sink pins still synthesized).
"""

from __future__ import annotations

import json
from pathlib import Path

from precis.pcb import easyeda

FIXTURE = Path(__file__).parent / "fixtures" / "pcb" / "easyeda_c42163081_trimmed.json"

#: One symbol pin primitive in the shape spike-verified 2026-09-17 against
#: C639448's cached raw doc: header field 3 is the pin NUMBER, the segment
#: ending in ``start`` carries the NAME, the one ending in ``end`` the
#: number again.
_SYMBOL_PIN_1 = (
    "P~show~0~1~335~105~180~gge41~0^^335~105^^M335,105h10~#880000"
    "^^1~348.7~109~0~HVOUT41~start~~~#0000FF^^1~344.5~104~0~1~end~~~#0000FF"
    "^^0~342~105^^0~M 345 108 L 348 105 L 345 102"
)
_SYMBOL_PIN_2 = (
    "P~show~0~2~335~115~180~gge42~0^^335~115^^M335,115h10~#880000"
    "^^1~348.7~119~0~DIN~start~~~#0000FF^^1~344.5~114~0~2~end~~~#0000FF"
)


def _doc_with_symbol(pins: list[object]) -> dict:
    doc = json.loads(FIXTURE.read_text(encoding="utf-8"))
    doc["result"]["dataStr"] = {"docType": 2, "shape": pins}
    return doc


def test_symbol_pin_names_parses_number_to_name():
    result = _doc_with_symbol([_SYMBOL_PIN_1, _SYMBOL_PIN_2, "TRACK~1~x", 42])["result"]
    assert easyeda._symbol_pin_names(result) == {"1": "HVOUT41", "2": "DIN"}


def test_symbol_pin_names_skips_unnamed_and_malformed_pins():
    same_as_number = (
        "P~show~0~3~0~0~0~gge43~0^^0~0^^M0,0h10~#880000^^1~0~0~0~3~start~~~#0"
    )
    result = _doc_with_symbol([same_as_number, "P~show"])["result"]
    assert easyeda._symbol_pin_names(result) == {}
    assert easyeda._symbol_pin_names({"dataStr": "not-a-dict"}) == {}
    assert easyeda._symbol_pin_names({}) == {}


def test_parse_component_pin_map_carries_symbol_names_when_present():
    footprint = easyeda.parse_component(
        _doc_with_symbol([_SYMBOL_PIN_1, _SYMBOL_PIN_2])
    )
    assert footprint is not None
    pin_map = footprint["pin_map"]
    assert pin_map["1"] == {"name": "HVOUT41", "tags": []}
    assert pin_map["2"] == {"name": "DIN", "tags": []}
    # pads the symbol does not name keep the number — the pre-existing default
    assert pin_map["3"] == {"name": "3", "tags": []}
    assert set(pin_map) == {"1", "2", "3", "4", "5", "6"}


def test_parse_component_without_a_symbol_keeps_number_names():
    doc = json.loads(FIXTURE.read_text(encoding="utf-8"))
    doc["result"].pop("dataStr", None)
    footprint = easyeda.parse_component(doc)
    assert footprint is not None
    assert footprint["pin_map"]["1"] == {"name": "1", "tags": []}

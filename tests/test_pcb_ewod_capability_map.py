"""pcb-ewod-multitile Slice 2 pt 2 — ``view='capability'``, a generator's
capability map (usable vs unusable/reserved pads, plaza slot allocation,
pin naming, computed sizing) off the ``pcb_generators`` ledger a
``generators:[...]`` apply already stored.

SVG geometry/legibility coverage (does it fit the canvas, does every text
line clear the viewBox) lives in the pure-Python unit test against
:mod:`precis.pcb.svg` directly; this file is the store/handler wiring
layer + the machine-readable ``format='ledger'`` table, matching
``test_pcb_ewod_generator.py``'s own split.
"""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.pcb import PcbHandler


@pytest.fixture
def pcb(store):
    return PcbHandler(hub=Hub(store=store))


def _array_args(name="ARR1", **params):
    return {
        "generators": [{"name": name, "generator": "ewod_pad_array", "params": params}]
    }


def test_capability_svg_view_renders_pins_and_summary(pcb):
    pcb.put(id="ewod-cap-1", args=_array_args(grid=[3, 3]))
    resp = pcb.get(id="ewod-cap-1", view="capability")
    assert "<svg" in resp.body
    assert "R0C0" in resp.body  # a pin label
    assert "8/8 usable" in resp.body  # summary line (3x3 full -- 8 electrodes)


def test_capability_ledger_view_lists_pads_and_plaza_slots(pcb):
    pcb.put(id="ewod-cap-1", args=_array_args(grid=[3, 3]))
    resp = pcb.get(id="ewod-cap-1", view="capability", args={"format": "ledger"})
    assert "R0C0" in resp.body
    assert "P1_1" in resp.body  # the one 3x3 plaza
    assert "used" in resp.body


def test_capability_view_reflects_reserved_and_unusable_pads(pcb):
    pcb.put(id="ewod-cap-1", args=_array_args(grid=[3, 3], reserve=["P1_1:N"]))
    svg_resp = pcb.get(id="ewod-cap-1", view="capability")
    assert "7/8 usable" in svg_resp.body
    ledger_resp = pcb.get(id="ewod-cap-1", view="capability", args={"format": "ledger"})
    assert "False" in ledger_resp.body or "false" in ledger_resp.body.lower()
    assert "reserved" in ledger_resp.body


def test_capability_view_needs_name_when_multiple_generators(pcb):
    pcb.put(
        id="ewod-cap-2",
        args={
            "generators": [
                {
                    "name": "ARR1",
                    "generator": "ewod_pad_array",
                    "params": {"grid": [3, 3], "x": -10},
                },
                {
                    "name": "ARR2",
                    "generator": "ewod_pad_array",
                    "params": {"grid": [3, 3], "x": 10},
                },
            ]
        },
    )
    with pytest.raises(BadInput, match="pass args={'name'"):
        pcb.get(id="ewod-cap-2", view="capability")
    resp = pcb.get(id="ewod-cap-2", view="capability", args={"name": "ARR2"})
    assert "<svg" in resp.body


def test_capability_view_unknown_generator_name_is_bad_input(pcb):
    pcb.put(id="ewod-cap-1", args=_array_args(grid=[3, 3]))
    with pytest.raises(BadInput, match="no generator named"):
        pcb.get(id="ewod-cap-1", view="capability", args={"name": "NOPE"})


def test_capability_view_with_no_generators_is_bad_input(pcb):
    pcb.put(
        id="ewod-cap-plain",
        args={
            "components": [{"refdes": "U1", "pins": [{"name": "A"}]}],
            "nets": [{"name": "N1"}],
            "connections": [{"net": "N1", "refdes": "U1", "pin": "A"}],
        },
    )
    with pytest.raises(BadInput, match="no generators on this design"):
        pcb.get(id="ewod-cap-plain", view="capability")


def test_capability_view_bad_format_is_bad_input(pcb):
    pcb.put(id="ewod-cap-1", args=_array_args(grid=[3, 3]))
    with pytest.raises(BadInput, match="not recognized"):
        pcb.get(id="ewod-cap-1", view="capability", args={"format": "pdf"})


def test_capability_view_shows_a_merged_pad_span(pcb):
    pcb.put(
        id="ewod-cap-merge",
        args=_array_args(grid=[3, 3], pad_sizes=[{"cells": [[0, 0], [0, 1]]}]),
    )
    resp = pcb.get(id="ewod-cap-merge", view="capability")
    assert "7/7 usable" in resp.body  # 8 electrodes -> 7 pads, one merged, all usable
    assert "R0C0" in resp.body
    assert "R0C1" not in resp.body
    ledger_resp = pcb.get(
        id="ewod-cap-merge", view="capability", args={"format": "ledger"}
    )
    assert "R0C0" in ledger_resp.body


def test_capability_view_works_for_rim_variant_too(pcb):
    pcb.put(id="ewod-cap-rim", args=_array_args(grid=[4, 4], variant="rim"))
    resp = pcb.get(id="ewod-cap-rim", view="capability")
    assert "<svg" in resp.body
    assert "12/12 usable" in resp.body

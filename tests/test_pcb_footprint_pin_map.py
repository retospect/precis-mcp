"""An authored footprint's ``pin_map`` is honoured, so a SEMANTIC pin name
reaches real copper without renaming the pad.

``_normalize_local_footprint`` used to build ``pin_map`` identity
unconditionally and silently discard any authored one. The only way to give
a pad the pin name ``"SCL"`` was therefore to call the *pad* ``"SCL"`` — and
``pcb_local_footprints``' pads keep that string as their pad ``number``,
which is exactly the field an EasyEDA export has to write back out. So the
two requirements were in direct conflict: name the pin, or keep the pad
number, never both.

This is the one store-layer change slice 1b of ``pcb-epro-import.md``
needs, and its acceptance criterion 3 ("a named pin ``U1.SCL`` resolves to
the right net, i.e. the ``pin_map`` join reaches real pads through
``padplace.board_pads``") is the second test below — asserting on placed
board copper rather than on the stored row, because the stored row agreeing
while the join misses is the failure that would ship.
"""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.pcb import PcbHandler
from precis.pcb import padplace

# Pad numbers stay "1"/"2"/"3" (what the source file calls them, and what a
# round-trip export must emit); the pin names are the semantic ones a
# netlist connects to. A bare string and the full {name, tags} row shape are
# both accepted -- an importer holding only a name should not have to know
# the row shape to use this.
_I2C_FOOTPRINT = {
    "name": "sot23_i2c",
    "pads": [
        {"pin": "1", "shape": "rect", "x": -0.95, "y": -0.5, "w": 0.6, "h": 0.7},
        {"pin": "2", "shape": "rect", "x": 0.95, "y": -0.5, "w": 0.6, "h": 0.7},
        {"pin": "3", "shape": "rect", "x": 0.0, "y": 0.95, "w": 0.6, "h": 0.7},
    ],
    "pin_map": {
        "1": "SCL",
        "2": {"name": "SDA", "tags": ["bidirectional"]},
        "3": "GND",
    },
}

_DESIGN = {
    "footprints": [_I2C_FOOTPRINT],
    "components": [
        {
            "refdes": "U1",
            "label": "I2C sensor",
            "footprint": "sot23_i2c",
            "x": 20.0,
            "y": 15.0,
            "pins": [{"name": "SCL"}, {"name": "SDA"}, {"name": "GND"}],
        }
    ],
    "nets": [{"name": "I2C_SCL"}, {"name": "I2C_SDA"}, {"name": "GND"}],
    "connections": [
        {"net": "I2C_SCL", "refdes": "U1", "pin": "SCL"},
        {"net": "I2C_SDA", "refdes": "U1", "pin": "SDA"},
        {"net": "GND", "refdes": "U1", "pin": "GND"},
    ],
}


@pytest.fixture
def pcb(store) -> PcbHandler:
    return PcbHandler(hub=Hub(store=store))


def _put(pcb: PcbHandler, store, slug: str, design: dict) -> int:
    """Returns the ref_id, which ``pcb_local_footprints_for`` keys on (the
    table is ref-scoped; the footprint NAME is only unique within a ref)."""
    pcb.put(id=slug, title=slug, args=design)
    ref = store.get_ref(kind="pcb", id=slug)
    assert ref is not None
    return int(ref.id)


def test_pin_map_is_stored_and_pad_numbers_survive(pcb: PcbHandler, store) -> None:
    ref_id = _put(pcb, store, "pinmap-1", _DESIGN)

    fp = store.pcb_local_footprints_for(ref_id)["sot23_i2c"]

    # The semantic names landed...
    assert fp["pin_map"]["1"]["name"] == "SCL"
    assert fp["pin_map"]["2"]["name"] == "SDA"
    assert fp["pin_map"]["2"]["tags"] == ["bidirectional"]
    assert fp["pin_map"]["3"]["name"] == "GND"

    # ...and the pad NUMBERS are untouched, which is the whole point: the
    # export half writes these back as EasyEDA pad numbers. Naming the pad
    # "SCL" would have satisfied the line above and destroyed this one.
    assert sorted(p["number"] for p in fp["pads"]) == ["1", "2", "3"]


def test_named_pin_reaches_real_copper_through_board_pads(
    pcb: PcbHandler, store
) -> None:
    """Acceptance criterion 3. ``pad_label`` resolves through ``pin_map``,
    and ``board_pads`` keys ``pin_to_net`` by the RESOLVED name -- so a pad
    carries its net only if the join actually went through the map."""
    ref_id = _put(pcb, store, "pinmap-2", _DESIGN)

    graph = store.pcb_graph(ref_id)
    pin_to_net = {
        (m["refdes"], m["pin"]): net["name"]
        for net in graph["nets"]
        for m in net["members"]
    }
    assert ("U1", "SCL") in pin_to_net, "netlist never recorded the named pin"

    pads, _drills = padplace.board_pads(
        graph["instances"],
        {},
        layers=["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"],
        pin_to_net=pin_to_net,
        local_footprints=store.pcb_local_footprints_for(ref_id),
    )

    by_pin = {p["pin"]: p for p in pads if p["refdes"] == "U1"}
    assert sorted(by_pin) == ["GND", "SCL", "SDA"], (
        "pads came back keyed by pad number, so pad_label never saw the map"
    )
    assert by_pin["SCL"]["net"] == "I2C_SCL"
    assert by_pin["SDA"]["net"] == "I2C_SDA"
    assert by_pin["GND"]["net"] == "GND"

    # Real copper, not a placeholder: the pad sits at the instance origin
    # plus its footprint-local offset.
    assert by_pin["SCL"]["x"] == pytest.approx(20.0 - 0.95, abs=1e-6)
    assert by_pin["SCL"]["y"] == pytest.approx(15.0 - 0.5, abs=1e-6)


def test_an_omitted_pad_keeps_its_number_as_its_pin_name(
    pcb: PcbHandler, store
) -> None:
    """A PARTIAL map names what it knows and leaves the rest numbered --
    the degradation an import with a half-resolved pin-name chain needs,
    rather than all-or-nothing."""
    partial = {
        "name": "partial_map",
        "pads": [
            {"pin": "1", "shape": "rect", "x": -0.5, "y": 0.0, "w": 0.4},
            {"pin": "2", "shape": "rect", "x": 0.5, "y": 0.0, "w": 0.4},
        ],
        "pin_map": {"1": "VDD"},
    }
    ref_id = _put(
        pcb,
        store,
        "pinmap-3",
        {
            "footprints": [partial],
            "components": [
                {
                    "refdes": "U2",
                    "footprint": "partial_map",
                    "x": 5.0,
                    "y": 5.0,
                    "pins": [{"name": "VDD"}, {"name": "2"}],
                }
            ],
        },
    )

    fp = store.pcb_local_footprints_for(ref_id)["partial_map"]
    assert fp["pin_map"]["1"]["name"] == "VDD"
    assert fp["pin_map"]["2"]["name"] == "2"


def test_a_pin_map_key_that_is_not_a_pad_number_is_refused(
    pcb: PcbHandler, store
) -> None:
    """The failure this guards is silent: a typo'd or stale key matches no
    pad, so the pin it meant to name falls back to the pad number with
    nothing said. Refusing names the offending key and the real ones."""
    bad = {
        "name": "bad_map",
        "pads": [{"pin": "1", "shape": "rect", "x": 0.0, "y": 0.0, "w": 0.4}],
        "pin_map": {"7": "NOPE"},
    }
    with pytest.raises(BadInput) as exc:
        _put(pcb, store, "pinmap-4", {"footprints": [bad]})
    msg = str(exc.value)
    assert "'7'" in msg and "not a pad number" in msg
    assert "'1'" in msg, "the message should name the pad numbers there ARE"


def test_a_pin_map_entry_with_no_name_is_refused(pcb: PcbHandler, store) -> None:
    bad = {
        "name": "empty_name",
        "pads": [{"pin": "1", "shape": "rect", "x": 0.0, "y": 0.0, "w": 0.4}],
        "pin_map": {"1": {"tags": ["x"]}},
    }
    with pytest.raises(BadInput) as exc:
        _put(pcb, store, "pinmap-5", {"footprints": [bad]})
    assert "needs a pin name" in str(exc.value)


def test_no_pin_map_is_still_identity(pcb: PcbHandler, store) -> None:
    """The pre-existing contract, unchanged -- every caller that authored a
    footprint before this change gets exactly what it got."""
    plain = {
        "name": "plain_fp",
        "pads": [
            {"pin": "A", "shape": "rect", "x": -0.5, "y": 0.0, "w": 0.4},
            {"pin": "B", "shape": "rect", "x": 0.5, "y": 0.0, "w": 0.4},
        ],
    }
    ref_id = _put(pcb, store, "pinmap-6", {"footprints": [plain]})

    fp = store.pcb_local_footprints_for(ref_id)["plain_fp"]
    assert fp["pin_map"] == {
        "A": {"name": "A", "tags": []},
        "B": {"name": "B", "tags": []},
    }

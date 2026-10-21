"""The net-label schematic renderer (:mod:`precis.pcb.schematic`).

Pure-function tests off a hand-built ``pcb_graph``-shaped dict — no store,
no placement (rendering before the first ``op='place'`` is the point).
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Any

from precis.pcb.schematic import render_schematic_svg


def _graph() -> dict[str, Any]:
    """A 3-part board: an IC, a passive, and a spare pin — every rendering
    case (signal label, ground glyph, power rail, no-connect) in one dict."""
    return {
        "instances": [
            {"refdes": "U1", "label": "MCU-QFN16", "n_pins": 4},
            {"refdes": "R1", "label": "RES-0402-10k", "n_pins": 2},
            {"refdes": "C1", "label": "CAP-0402-100n", "n_pins": 2},
        ],
        "nets": [
            {
                "name": "GND",
                "net_class": "ground",
                "members": [
                    {"refdes": "U1", "pin": "3"},
                    {"refdes": "C1", "pin": "2"},
                ],
            },
            {
                "name": "5V",
                "net_class": "power",
                "members": [
                    {"refdes": "U1", "pin": "4"},
                    {"refdes": "C1", "pin": "1"},
                ],
            },
            {
                "name": "SDA",
                "net_class": None,
                "members": [
                    {"refdes": "U1", "pin": "1"},
                    {"refdes": "R1", "pin": "1"},
                ],
            },
            {
                "name": "SCL",
                "net_class": None,
                "members": [
                    {"refdes": "U1", "pin": "2"},
                    {"refdes": "R1", "pin": "2"},
                ],
            },
        ],
        "unconnected": [{"refdes": "U1", "pin": "10"}],
    }


def test_mixed_named_and_numeric_pins_sort_without_crash() -> None:
    """Imported (EasyEDA) parts carry named pins next to numeric ones —
    'A1' beside '1'. The pin sort must not compare int < str (gr496856:
    the schematic view TypeError'd on taz15-r2)."""
    graph = {
        "instances": [{"refdes": "Q1", "label": "MOSFET", "n_pins": 3}],
        "nets": [
            {
                "name": "SIG",
                "net_class": None,
                "members": [{"refdes": "Q1", "pin": "A1"}],
            },
            {
                "name": "GND",
                "net_class": "ground",
                "members": [{"refdes": "Q1", "pin": "1"}],
            },
        ],
        "unconnected": [{"refdes": "Q1", "pin": "S"}],
    }
    svg = render_schematic_svg(graph, title="t")
    assert ">Q1</text>" in svg and ">SIG</text>" in svg


def test_every_part_pin_and_net_label_appears() -> None:
    svg = render_schematic_svg(_graph(), title="t")
    for refdes in ("U1", "R1", "C1"):
        assert f">{refdes}</text>" in svg
    # signal nets appear as labels at BOTH their ends
    assert svg.count(">SDA</text>") == 2
    assert svg.count(">SCL</text>") == 2
    # the power rail label appears at both its ends too
    assert svg.count(">5V</text>") == 2


def test_ground_renders_as_glyph_not_text() -> None:
    svg = render_schematic_svg(_graph(), title="t")
    # ground is the three-bar glyph — no GND text label anywhere
    assert ">GND</text>" not in svg
    # ...but the hover title still says the net name and members
    assert "net GND — U1.3, C1.2" in svg


def test_unconnected_pin_gets_the_no_connect_mark() -> None:
    svg = render_schematic_svg(_graph(), title="t")
    assert 'class="nc"' in svg
    assert "U1.10 — no connect" in svg


def test_render_is_deterministic_and_well_formed_xml() -> None:
    a = render_schematic_svg(_graph(), title="t")
    b = render_schematic_svg(_graph(), title="t")
    assert a == b
    root = ET.fromstring(a)
    assert root.tag.endswith("svg")


def test_renders_with_no_placement_and_no_nets() -> None:
    """A just-created design (parts only, nothing wired, nothing placed)
    still renders — every pin is a no-connect."""
    svg = render_schematic_svg(
        {
            "instances": [{"refdes": "J1", "label": "HDR", "n_pins": 2}],
            "nets": [],
            "unconnected": [
                {"refdes": "J1", "pin": "1"},
                {"refdes": "J1", "pin": "2"},
            ],
        },
        title="bare",
    )
    assert ">J1</text>" in svg
    assert svg.count('class="nc"') == 2


def test_natural_pin_order_puts_2_before_10() -> None:
    """Pin '2' sorts before '10' (datasheet order, not ASCII)."""
    svg = render_schematic_svg(
        {
            "instances": [{"refdes": "U9", "label": "X", "n_pins": 3}],
            "nets": [],
            "unconnected": [
                {"refdes": "U9", "pin": "10"},
                {"refdes": "U9", "pin": "2"},
                {"refdes": "U9", "pin": "1"},
            ],
        },
        title="t",
    )
    assert svg.index(">1</text>") < svg.index(">2</text>") < svg.index(">10</text>")


def test_no_focus_dims_nothing() -> None:
    # the .dimmed rule is always in the <style>; assert no part USES it
    assert 'class="sch-part dimmed"' not in render_schematic_svg(_graph(), title="t")


def test_focus_on_a_part_dims_outside_the_one_hop_neighbourhood() -> None:
    """Focus U1: its signal-net neighbour R1 (SDA/SCL) stays bright, C1
    dims. C1 shares only GND and 5V with U1 — focus does NOT expand through
    power/ground (that would flood the view), the same rule the click
    cross-probe follows. Positions are unchanged; only a class differs."""
    svg = render_schematic_svg(_graph(), title="t", focus="U1")
    assert '<g class="sch-part focused" data-handle="U1"' in svg
    assert '<g class="sch-part" data-handle="R1"' in svg
    assert '<g class="sch-part dimmed" data-handle="C1"' in svg
    # the heading names the focus target
    assert "focus U1" in svg


def test_focus_on_a_pin_focuses_its_part() -> None:
    svg = render_schematic_svg(_graph(), title="t", focus="U1.1")
    assert '<g class="sch-part focused" data-handle="U1"' in svg
    assert '<g class="sch-part dimmed" data-handle="C1"' in svg


def test_focus_on_a_net_brightens_its_members_only() -> None:
    """``net:SDA`` has members U1 and R1; C1 is not on it, so C1 dims. A net
    focus has no single focused part, so no part gets the 'focused' class."""
    svg = render_schematic_svg(_graph(), title="t", focus="net:SDA")
    assert '<g class="sch-part" data-handle="U1"' in svg
    assert '<g class="sch-part" data-handle="R1"' in svg
    assert '<g class="sch-part dimmed" data-handle="C1"' in svg


def test_unknown_focus_target_is_a_no_op() -> None:
    assert 'class="sch-part dimmed"' not in render_schematic_svg(
        _graph(), title="t", focus="U9"
    )
    assert 'class="sch-part dimmed"' not in render_schematic_svg(
        _graph(), title="t", focus="net:X"
    )


def test_a_fixed_instance_is_drawn_pinned() -> None:
    """A placement-locked instance (``pcb_instances.fixed``) is stamped
    ``data-pinned`` and draws the pushpin, so the pin shows after a reload.
    An unlocked part is not pinned."""
    graph = _graph()
    graph["instances"][0]["fixed"] = "xy"  # U1 locked
    svg = render_schematic_svg(graph, title="t")
    assert 'data-handle="U1" data-pcb-part="U1" data-pinned="1"' in svg
    assert 'class="pin-mark"' in svg
    assert 'data-handle="R1" data-pcb-part="R1" data-pinned' not in svg


def test_hostile_slug_cannot_escape_the_aria_label_attribute() -> None:
    """``title`` is the raw agent-supplied design slug and lands in an
    ATTRIBUTE (aria-label). ``xml.sax.saxutils.escape`` alone leaves ``"``
    intact, so ``x" onload="..."`` would close the attribute and add a live
    event handler on the <svg> root -- it fires on top-level navigation to
    /pcb/{slug}/schematic.svg."""
    hostile = 'x" onload="alert(1)'
    svg = render_schematic_svg(_graph(), title=hostile)
    root = ET.fromstring(svg)
    assert root.get("onload") is None
    assert root.get("aria-label") == hostile  # round-trips as plain text

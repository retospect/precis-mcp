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


# ── sheets (pcb-workbench-layout.md slice 2) ─────────────────────────


def _grouped_graph() -> dict[str, Any]:
    """Two placement groups (MCU, power) plus an ungrouped part, wired so
    SIG crosses the MCU↔power sheets and TAIL runs from MCU to the
    ungrouped part; GND and 5V are global."""
    return {
        "instances": [
            {"refdes": "U1", "label": "MCU-QFN16", "n_pins": 4, "group": "MCU"},
            {"refdes": "R1", "label": "RES-0402-10k", "n_pins": 2, "group": "power"},
            {"refdes": "C1", "label": "CAP-0402-100n", "n_pins": 2, "group": "power"},
            {"refdes": "J1", "label": "HDR", "n_pins": 2},
        ],
        "nets": [
            {
                "name": "GND",
                "net_class": "ground",
                "members": [
                    {"refdes": "U1", "pin": "3"},
                    {"refdes": "C1", "pin": "2"},
                    {"refdes": "J1", "pin": "2"},
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
                "name": "SIG",
                "net_class": None,
                "members": [
                    {"refdes": "U1", "pin": "1"},
                    {"refdes": "R1", "pin": "1"},
                ],
            },
            {
                "name": "TAIL",
                "net_class": None,
                "members": [
                    {"refdes": "U1", "pin": "2"},
                    {"refdes": "J1", "pin": "1"},
                ],
            },
        ],
        "unconnected": [],
    }


def test_sheet_names_come_from_placement_groups() -> None:
    from precis.pcb.schematic import sheet_map, sheet_names

    graph = _grouped_graph()
    assert sheet_names(graph) == ["MCU", "power"]
    assert sheet_map(graph) == {"U1": "MCU", "R1": "power", "C1": "power"}


def test_a_sheet_renders_only_its_groups_parts() -> None:
    svg = render_schematic_svg(_grouped_graph(), title="t", sheet="MCU")
    assert ">U1</text>" in svg
    assert ">R1</text>" not in svg
    assert ">C1</text>" not in svg
    assert ">J1</text>" not in svg
    assert "sheet MCU" in svg


def test_cross_sheet_net_gets_connectors_on_both_sheets() -> None:
    """The spec's exit: SIG joins U1 (MCU) to R1 (power). On the MCU
    sheet its stub ends in an off-sheet connector naming power; on the
    power sheet, one naming MCU."""
    mcu = render_schematic_svg(_grouped_graph(), title="t", sheet="MCU")
    power = render_schematic_svg(_grouped_graph(), title="t", sheet="power")
    assert 'class="offsheet"' in mcu and 'class="offsheet"' in power
    assert ">SIG " in mcu and 'data-sheet="power"' in mcu and "→ power" in mcu
    assert ">SIG " in power and 'data-sheet="MCU"' in power and "→ MCU" in power
    # ...while the overview shows the whole net as plain labels, no flags
    overview = render_schematic_svg(_grouped_graph(), title="t")
    assert 'class="offsheet"' not in overview
    assert ">SIG</text>" in overview


def test_net_to_an_ungrouped_part_points_at_the_overview() -> None:
    svg = render_schematic_svg(_grouped_graph(), title="t", sheet="MCU")
    assert ">TAIL " in svg and 'data-sheet=""' in svg and "→ overview<" in svg


def test_offsheet_multi_dest_gets_an_arrow_each_not_a_comma() -> None:
    """gr520804's last open item: ``VSTOR → boost, overview`` read as a
    comma list after one arrow. Each destination now draws its own
    arrow — ``VSTOR → boost → overview`` — inside its own data-sheet
    tspan, so a multi-sheet net reads as separate click targets."""
    from precis.pcb.schematic import _offsheet_glyph

    glyph = _offsheet_glyph(10.0, 20.0, "VSTOR", ["boost", "overview"], rightward=True)
    assert ">→ boost</tspan>" in glyph
    assert ">→ overview</tspan>" in glyph
    assert ", " not in glyph.split("</title>")[-1]
    assert glyph.count('class="offsheet-dest"') == 2


def test_offsheet_flag_explains_itself_and_each_dest_is_clickable() -> None:
    """gr520804 — the flag was one flat ``NET → a, b`` run: unreadable
    (no explanation anywhere), unclickable (a click selected the net and
    the sheet logic stayed put), and multi-destination sheets were just
    comma text. Now the flag carries a <title> saying what it is and
    every destination is its own data-sheet tspan the workbench's click
    wiring reads to switch sheets; 'overview' maps to '' (pcb-sheets.js's
    own key for the catch-all tab)."""
    svg = render_schematic_svg(_grouped_graph(), title="t", sheet="MCU")
    assert "off-sheet net SIG" in svg
    assert "click a destination to open that sheet" in svg
    assert '<tspan data-sheet="power" class="offsheet-dest">' in svg
    assert "continues on sheet power" in svg
    # The dest tspan for the overview carries pcb-sheets.js's '' key.
    assert '<tspan data-sheet="" class="offsheet-dest">' in svg
    assert "continues on sheet overview" in svg


def test_power_and_ground_never_get_connectors() -> None:
    """Power/ground are global symbols (KiCad's rule, and slice 1's
    flood rule) — no connector even though every sheet holds members."""
    for sheet in ("MCU", "power"):
        svg = render_schematic_svg(_grouped_graph(), title="t", sheet=sheet)
        assert ">5V → " not in svg
        assert ">GND → " not in svg


def test_same_sheet_net_keeps_a_plain_label() -> None:
    """A net entirely inside one sheet is not a cross-sheet net."""
    graph = _grouped_graph()
    graph["nets"].append(
        {
            "name": "LOCAL",
            "net_class": None,
            "members": [
                {"refdes": "R1", "pin": "2"},
                {"refdes": "C1", "pin": "1"},
            ],
        }
    )
    svg = render_schematic_svg(graph, title="t", sheet="power")
    assert ">LOCAL → " not in svg


def test_unknown_sheet_falls_back_to_the_overview() -> None:
    svg = render_schematic_svg(_grouped_graph(), title="t", sheet="nope")
    assert ">U1</text>" in svg and ">J1</text>" in svg
    assert "sheet nope" not in svg


def test_sheet_render_stays_deterministic_and_well_formed() -> None:
    a = render_schematic_svg(_grouped_graph(), title="t", sheet="MCU")
    b = render_schematic_svg(_grouped_graph(), title="t", sheet="MCU")
    assert a == b
    ET.fromstring(a)


def test_only_drops_dimmed_parts_and_off_focus_pins() -> None:
    """gr520807: ``only`` trims to the focus's one-hop set outright —
    no dimmed shell of the rest, and a kept neighbour shows only the
    pins tying it to the focus (the focus part keeps all of its own)."""
    svg = render_schematic_svg(_graph(), title="t", focus="U1", only=True)
    assert 'data-handle="C1"' not in svg  # dropped, not dimmed
    assert "sch-part dimmed" not in svg
    assert '<g class="sch-part focused" data-handle="U1"' in svg
    assert 'data-handle="R1.1"' in svg and 'data-handle="R1.2"' in svg
    assert 'data-handle="U1.3"' in svg  # the focus part keeps every pin
    assert "connected only" in svg


def test_only_on_a_net_focus_keeps_just_that_nets_pins() -> None:
    svg = render_schematic_svg(_graph(), title="t", focus="net:SDA", only=True)
    assert 'data-handle="C1"' not in svg
    assert 'data-handle="U1.1"' in svg
    assert 'data-handle="U1.2"' not in svg  # the SCL pin drops under net:SDA
    assert 'data-handle="R1.1"' in svg
    assert 'data-handle="R1.2"' not in svg


def test_only_without_a_focus_is_a_no_op() -> None:
    assert render_schematic_svg(_graph(), title="t", only=True) == (
        render_schematic_svg(_graph(), title="t")
    )
    svg = render_schematic_svg(_graph(), title="t", focus="U9", only=True)
    assert 'data-handle="C1"' in svg  # unknown focus dims/drops nothing


def test_pin_numbers_draw_only_when_they_differ_from_the_name() -> None:
    """gr520803: the pad number rides above the stub at the box edge when
    the pin's name is NOT its number ('VSS' → pad 7); a pin named '1'
    already says it, so nothing draws twice. No map, no labels."""
    graph = {
        "instances": [{"refdes": "U2", "label": "PMIC", "n_pins": 2}],
        "nets": [
            {
                "name": "GND",
                "net_class": "ground",
                "members": [{"refdes": "U2", "pin": "VSS"}],
            },
            {
                "name": "SIG",
                "net_class": None,
                "members": [{"refdes": "U2", "pin": "1"}],
            },
        ],
        "unconnected": [],
    }
    svg = render_schematic_svg(
        graph, title="t", pin_numbers={"U2": {"VSS": "7", "1": "1"}}
    )
    assert 'class="pinno">7</text>' in svg
    assert svg.count('class="pinno"') == 1  # pin '1' draws no duplicate
    assert 'class="pinno"' not in render_schematic_svg(graph, title="t")


def _declared_graph() -> dict[str, Any]:
    """Pins with etypes/meta/pad, a net with the spec fields, and a part
    with pin_decls — the gr520809 hover/glyph material."""
    return {
        "instances": [
            {
                "refdes": "U2",
                "label": "PMIC",
                "n_pins": 4,
                "pin_decls": {
                    "pin_functions": {
                        "VSTOR": {
                            "functions": ["GPIO", "AIN"],
                            "max_current_a": 0.4,
                        },
                        "EN": ["GPIO"],
                    },
                    "slots": [{"name": "buck", "pins": ["EN", "VSTOR"]}],
                },
            },
        ],
        "nets": [
            {
                "name": "PWR",
                "net_class": "power",
                "est_current_a": 0.4,
                "working_voltage_v": 5,
                "impedance_ohm": 50,
                "edge_rate_v_per_ns": 2,
                "function_hint": "buck output",
                "members": [
                    {
                        "refdes": "U2",
                        "pin": "VSTOR",
                        "tags": ["power_out"],
                        "meta": {"max_voltage_v": 5.5},
                        "pad": "4",
                    },
                    {"refdes": "U2", "pin": "EN", "tags": ["input"]},
                ],
            },
        ],
        "unconnected": [
            {"refdes": "U2", "pin": "NC1", "tags": ["nc"]},
            {"refdes": "U2", "pin": "AUX"},
        ],
    }


def test_every_pin_carries_a_direction_mark_and_unknown_shows() -> None:
    """gr520809 (1): every stub gets its §1i direction glyph — chevron,
    diamond, ×, etc. — and an undeclared pin gets an explicit '?'
    because "direction unknown" must be visible, never absent."""
    svg = render_schematic_svg(_declared_graph(), title="t")
    # VSTOR power_out (chevron + tip bar = 2 paths), EN input (1),
    # NC1 nc × (1); AUX undeclared draws '?' text instead
    assert svg.count('class="pindir"') == 4
    assert svg.count('class="pindirt">?</text>') == 1


def test_stub_title_carries_etype_pad_spec_bounds_functions_slot() -> None:
    """gr520809 (2): the pin mouseover keeps the net line verbatim and
    adds the pin's etype/pad, the net's declared spec, and the pin's
    bounds / functions / flex slot — unknown etype says so."""
    svg = render_schematic_svg(_declared_graph(), title="t")
    assert "net PWR — U2.VSTOR, U2.EN" in svg  # the old line, verbatim
    assert "U2.VSTOR (pad 4) — power_out" in svg
    assert "U2.EN — input" in svg
    assert "U2.AUX — direction unknown" in svg
    assert (
        "net PWR — class power · est 0.4A · working 5V · 50Ω · "
        "edge 2V/ns · buck output" in svg
    )
    assert "max_voltage_v=5.5" in svg
    assert "functions GPIO, AIN (max 0.4A)" in svg
    assert "flex slot buck" in svg


def test_stub_title_keeps_the_no_connect_line_first() -> None:
    svg = render_schematic_svg(_graph(), title="t")
    assert "U1.10 — no connect" in svg
    assert "U1.10 — direction unknown" in svg


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

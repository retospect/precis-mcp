"""``ewod-dogfood-1`` — the round-1 EWOD dogfood vehicle (docs/backlog/
pcb-ewod-multitile.md's "Dogfood vehicle" section): 8x8 pad field @2.25mm
pitch, 250V drive (docs/backlog "Rulings 2026-09-19" — IPC-2221B B4 at
101-300V is 0.4mm, min_pitch 2.233mm; the field previously carried no
declared voltage at all, falling back to the fab spacing floor), one
merged reservoir pad, one HV507-class 64-channel sink under the whole
array (``sink_grid``, round 7), a bottom-side I2C temperature sensor, a
top-plate pogo terminal, an HV-in connector + bleed resistor, and a plain
pin header out to the instrument.

**Test-fixture footprints, not datasheet-accurate ones.** Every cached
"part" footprint below is a hand-authored SYNTHETIC grid of rectangular
pads (:func:`_grid_footprint`) named to match this test's own
``sink_grid``/``connections`` wiring exactly — the same trimming the
existing reference fixtures already do (``tests/test_pcb_fab_export.py``'s
``_QFN_FOOTPRINT`` trims a real 32-pin QFN down to 3 pads it actually
exercises). No network fetch happens; nothing here is a BOM commitment —
swap in a real ``ensure_footprint``-pulled/verified row before ordering.
LCSC C-numbers are the spec's own choice (HV507PG-G = C639448) or a
documented placeholder for an unverified "any in-stock I2C temp sensor"
pick — see each constant's own comment.

**Engine gaps this board found, all closed** (each is pinned by a test
below so it stays closed):

1. **The escape gap (round 8 → closed by island terminals + gripe
   346962).** The IR carries one position per PIN, so an electrode's
   authored plaza VIA — the escape this design is built around — was
   invisible to the router; island terminals from fixed copper made it
   a terminal, and then the maze's enclosing-DISC pad claims (wider
   than the pitch) still walled every F.Cu cell off until true-shape
   claims replaced them. ``test_dogfood_route_op_routes_real_geometry_
   and_reports_the_escape_gap`` asserts escapes now route through the
   fabric and that any remaining failure (the congestion race, gripe
   347037) keeps a recorded reason.

2. **gr341516, closed:** the sink sits directly under the array by design
(the whole point of ``sink_grid``) and used to be checked as if it were
on top — ``rules.py::PAD_LAYER`` forced every pad's IR layer to 0
regardless of the instance's real ``layer='bottom'`` side, so courtyard/
clearance checks had no way to know the sink and the array are on
OPPOSITE physical sides. Fixed: the IR now carries a real per-instance
side (:attr:`precis.pcb.ir.PcbIR.inst_bottom`), and
:func:`precis.pcb.realize.pads_for_ir` emits each pad's own outer layer
off it. ``test_dogfood_drc_view_findings_are_all_the_documented_side_gap``
now asserts the array-vs-sink pair produces ZERO cross-layer findings,
not merely that the array's own geometry is clean.

(Round 7's much larger third gap — synthesized per-pin POSITIONS reaching
DRC/routing/connectivity while pad SIZE came from the real footprint,
gripe 338983 — is FIXED as of round 8: ``precis.pcb.session.
apply_real_pin_offsets``, wired into ``build_ir``. The pad-source
agreement it restored is pinned directly by
``test_dogfood_drc_pads_sit_where_the_exported_copper_does``.)
"""

from __future__ import annotations

import math
import re
import zipfile
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.handlers.pcb import PcbHandler

# HV507PG-G, 64-channel push-pull HV shift register (docs/backlog/
# pcb-ewod-multitile.md's own dogfood choice, "the OpenDrop-proven EWOD
# part"). 80-lead PQFP for real; trimmed here to the 64 channels +
# DIN/DOUT/VDD/GND/VPP this test actually wires (69 of 80 real pins).
_HV507_LCSC = "C639448"
_HV507_CHANNELS = [f"OUT{i}" for i in range(64)]
_HV507_PINS = [*_HV507_CHANNELS, "DIN", "DOUT", "VDD", "GND", "VPP"]

# Placeholder LCSC C-number for a basic-parts I2C temp sensor (an
# LM75-class SOIC-8 part, per the spec's own "any in-stock LCSC
# basic-parts I2C temp sensor" latitude) -- NOT live-verified against
# current LCSC stock; this is a layout exercise, not a BOM commitment.
_TEMP_SENSOR_LCSC = "C32254"
_TEMP_SENSOR_PINS = ["VDD", "GND", "SCL", "SDA"]


def _grid_footprint(
    pin_names: list[str],
    *,
    cols: int,
    pitch: float = 1.0,
    pad_w: float = 0.5,
    pad_h: float = 0.5,
) -> dict[str, Any]:
    """A synthetic EasyEDA-shaped footprint (the ``{pads, pin_map}`` cache
    row :func:`precis.pcb.easyeda.parse_component` would normally produce)
    for a part this test invents: one rectangular pad per name, laid out
    in a plain grid (NOT the part's real package outline -- see module
    docstring). Good enough to exercise real pad geometry through
    placement/DRC/gerber without needing a network fetch."""
    pads = []
    pin_map = {}
    for i, pin in enumerate(pin_names):
        r, c = divmod(i, cols)
        number = str(i + 1)
        pads.append(
            {
                "number": number,
                "shape": "RECT",
                "x": c * pitch,
                "y": r * pitch,
                "w": pad_w,
                "h": pad_h,
                "rot": 0.0,
                "layer": "F.Cu",
                "drill": None,
            }
        )
        pin_map[number] = {"name": pin, "tags": []}
    return {"pads": pads, "pin_map": pin_map}


@pytest.fixture
def pcb(store):
    return PcbHandler(hub=Hub(store=store))


def _design() -> dict[str, Any]:
    return {
        "generators": [
            {
                "name": "ARR1",
                "generator": "ewod_pad_array",
                "params": {
                    "grid": [8, 8],
                    # Rulings 2026-09-19 items 3/10: a declared drive
                    # voltage derives `hv_separation` from IPC-2221B's B4
                    # column instead of falling back to the fab spacing
                    # floor; 250V's own min_pitch (2.233mm) is why `pitch`
                    # is declared explicitly too (the module default alone,
                    # 2.25mm, would already clear it, but the ruling's own
                    # numbers are worth pinning here for the test's sake).
                    "drive_voltage_v": 250,
                    "pitch": 2.25,
                    # One merged reservoir pad, corner cells (0,0)-(0,1) --
                    # neither is a via-plaza cell (plazas sit at r,c in
                    # {1,4,7}), so the "never cover a plaza" rule is clear.
                    "pad_sizes": [{"name": "RESV", "cells": [[0, 0], [0, 1]]}],
                    "sink_grid": {
                        "part": _HV507_LCSC,
                        "channels_per_sink": 64,  # one sink for the whole 8x8 field
                        "channel_pins": _HV507_CHANNELS,
                        "serial_in_pin": "DIN",
                        "serial_out_pin": "DOUT",
                        "top_plate_pin": "VPP",  # complement/top-plate rail
                        "power": {"VDD": "VDD_LOGIC", "GND": "GND"},
                    },
                },
            }
        ],
        "footprints": [
            {
                # Top-plate pogo terminal (design-review item 1): the real
                # pogo part is an open spec item -- plain through-hole pad
                # RINGS stand in for it (TODO: swap for a real low-profile
                # recessed pogo part once found). All 4 pads share ONE pin
                # (they are electrically the same top-plate contact, just
                # physically redundant), same "two pads, one pin" pattern
                # the array's own stub+via pairs already use.
                "name": "pogo-ring-4",
                "pads": [
                    {
                        "pin": "TOP",
                        "shape": "circle",
                        "x": x,
                        "y": y,
                        "w": 1.2,
                        "drill": 0.6,
                    }
                    for x, y in ((-2.0, -2.0), (2.0, -2.0), (-2.0, 2.0), (2.0, 2.0))
                ],
            },
            {
                "name": "hv-in-connector",
                "pads": [
                    {
                        "pin": "HV+",
                        "shape": "circle",
                        "x": -1.5,
                        "y": 0.0,
                        "w": 1.6,
                        "drill": 1.0,
                    },
                    {
                        "pin": "HV-",
                        "shape": "circle",
                        "x": 1.5,
                        "y": 0.0,
                        "w": 1.6,
                        "drill": 1.0,
                    },
                ],
            },
            {
                "name": "bleed-resistor-0805",
                "pads": [
                    {
                        "pin": "1",
                        "shape": "rect",
                        "x": -0.95,
                        "y": 0.0,
                        "w": 1.0,
                        "h": 1.25,
                    },
                    {
                        "pin": "2",
                        "shape": "rect",
                        "x": 0.95,
                        "y": 0.0,
                        "w": 1.0,
                        "h": 1.25,
                    },
                ],
            },
            {
                "name": "pinheader-4",
                "pads": [
                    {
                        "pin": str(i + 1),
                        "shape": "circle",
                        "x": i * 2.54,
                        "y": 0.0,
                        "w": 1.2,
                        "drill": 0.8,
                    }
                    for i in range(4)
                ],
            },
            {
                "name": "serial-header-2",
                "pads": [
                    {
                        "pin": "1",
                        "shape": "circle",
                        "x": 0.0,
                        "y": 0.0,
                        "w": 1.2,
                        "drill": 0.8,
                    },
                    {
                        "pin": "2",
                        "shape": "circle",
                        "x": 2.54,
                        "y": 0.0,
                        "w": 1.2,
                        "drill": 0.8,
                    },
                ],
            },
        ],
        "components": [
            {
                "refdes": "U_TEMP",
                "label": "I2C temp sensor (LM75-class, placeholder C-number)",
                "part": _TEMP_SENSOR_LCSC,
                "footprint": "SOIC-8",
                "layer": "bottom",
                "x": 20.0,
                "y": 0.0,
                "fixed": "both",
                "pins": [{"name": p} for p in _TEMP_SENSOR_PINS],
            },
            {
                "refdes": "POGO1",
                "label": "Top-plate pogo terminal (TODO: real part)",
                "footprint": "pogo-ring-4",
                "x": 20.0,
                "y": 10.0,
                "fixed": "both",
                "pins": [{"name": "TOP"}],
            },
            {
                "refdes": "J_HV",
                "label": "HV-in connector",
                "footprint": "hv-in-connector",
                "x": 20.0,
                "y": -10.0,
                "fixed": "both",
                "pins": [{"name": "HV+"}, {"name": "HV-"}],
            },
            {
                "refdes": "R_BLEED",
                "label": "HV rail bleed resistor",
                "footprint": "bleed-resistor-0805",
                "x": 20.0,
                "y": -6.0,
                "fixed": "both",
                "pins": [{"name": "1"}, {"name": "2"}],
            },
            {
                "refdes": "J_INSTR",
                "label": "Instrument pin header (power + I2C)",
                "footprint": "pinheader-4",
                "x": 20.0,
                "y": 5.0,
                "fixed": "both",
                "pins": [{"name": str(i + 1)} for i in range(4)],
            },
            {
                "refdes": "J_SERIAL",
                "label": "Serial bus out header (DIN/DOUT)",
                "footprint": "serial-header-2",
                "x": 20.0,
                "y": 15.0,
                "fixed": "both",
                "pins": [{"name": "1"}, {"name": "2"}],
            },
        ],
        "nets": [
            {"name": "HV_RAIL"},
            {"name": "GND"},
            {"name": "VDD_LOGIC"},
            {"name": "I2C_SCL"},
            {"name": "I2C_SDA"},
        ],
        "connections": [
            {"net": "HV_RAIL", "refdes": "J_HV", "pin": "HV+"},
            {"net": "HV_RAIL", "refdes": "R_BLEED", "pin": "1"},
            {"net": "HV_RAIL", "refdes": "J_INSTR", "pin": "1"},
            {"net": "GND", "refdes": "J_HV", "pin": "HV-"},
            {"net": "GND", "refdes": "R_BLEED", "pin": "2"},
            {"net": "GND", "refdes": "U_TEMP", "pin": "GND"},
            {"net": "GND", "refdes": "J_INSTR", "pin": "2"},
            {"net": "VDD_LOGIC", "refdes": "U_TEMP", "pin": "VDD"},
            {"net": "I2C_SCL", "refdes": "U_TEMP", "pin": "SCL"},
            {"net": "I2C_SCL", "refdes": "J_INSTR", "pin": "3"},
            {"net": "I2C_SDA", "refdes": "U_TEMP", "pin": "SDA"},
            {"net": "I2C_SDA", "refdes": "J_INSTR", "pin": "4"},
            # sink_grid's own externally-facing serial-chain net names
            # (single sink -> "ARR1_serial_in" in, "ARR1_serial_0_0" out).
            {"net": "ARR1_serial_in", "refdes": "J_SERIAL", "pin": "1"},
            {"net": "ARR1_serial_0_0", "refdes": "J_SERIAL", "pin": "2"},
            # sink_grid's own shared top-plate rail -> the pogo terminal.
            {"net": "ARR1_top_plate", "refdes": "POGO1", "pin": "TOP"},
        ],
        "features": [
            {
                "ftype": "outline",
                "geom": {"path": [[-15, -15], [35, -15], [35, 25], [-15, 25]]},
            },
        ],
    }


def _seed(pcb) -> str:
    design = _design()
    pcb.put(id="ewod-dogfood-1", args=design)
    pcb.store.part_footprint_put(_HV507_LCSC, _grid_footprint(_HV507_PINS, cols=9))
    pcb.store.part_footprint_put(
        _TEMP_SENSOR_LCSC, _grid_footprint(_TEMP_SENSOR_PINS, cols=4)
    )
    return "ewod-dogfood-1"


def test_dogfood_applies_and_places_69_channel_sink_under_the_array(pcb):
    slug = _seed(pcb)
    ref = pcb.store.get_ref(kind="pcb", id=slug)
    assert ref is not None
    graph = pcb.store.pcb_graph(ref.id)
    by_refdes = {i["refdes"]: i for i in graph["instances"]}
    assert "ARR1_SINK_0" in by_refdes
    assert by_refdes["ARR1_SINK_0"]["layer"] == "bottom"
    # Centroid of its own share (ruling 2026-09-18, "balanced by chain
    # order") -- not exactly the field's own geometric centre, since the
    # merged RESV pad (a top-left corner span) drops one electrode's own
    # chain position asymmetrically, but still well inside the field,
    # "directly under the array" either way.
    assert by_refdes["ARR1_SINK_0"]["x"] == pytest.approx(0.0, abs=1.0)
    assert by_refdes["ARR1_SINK_0"]["y"] == pytest.approx(0.0, abs=1.0)
    # 64 pads - 9 auto plazas - 1 (the RESV merge removes one net) = 54
    # distinct escape pins on an 8x8 field; the merged RESV pad plus
    # whichever boundary cells lack an adjacent plaza are excluded from
    # the sink's own channel roster (see the ledger assertion below).
    gens = pcb.store.pcb_generators_for(ref.id)
    ledger = gens["ARR1"]["ledger"]
    assert ledger["sink_grid"]["ARR1_SINK_0"]["channels"]


@pytest.mark.slow
def test_dogfood_drc_view_findings_are_all_the_documented_side_gap(pcb, store):
    """Round 8: ``view='drc'`` on this board is SIGNAL now, not noise.

    Round 7 could only pin a floor ("some findings exist") because every
    clearance measurement was taken between wrongly-anchored polygons
    (gripe 338983). With real pin positions on the IR, each finding is a
    genuine geometry fact, so this test asserts WHICH facts they are —
    the strong form: NOTHING is an array-internal electrode-to-electrode
    finding, and (gr341516, closed — module docstring's own "gr341516,
    closed" note) NOTHING is an array-vs-sink finding either, now that the
    IR/DRC path knows the sink sits on the OPPOSITE physical side.
    ``test_dogfood_applies_and_places_69_channel_sink_under_the_array``
    already pins that the sink really is ``layer='bottom'`` in the
    design; this is the DRC-side consequence of that fact.

    The array-internal clause is the older, still load-bearing one: the
    array's own zigzag geometry, plaza rings and stub tapers were proved
    clean by ``tests/test_pcb_ewod_generator_drc.py`` against the
    ``board_pads`` export path — this asserts the IR/DRC path now agrees
    with it on a real board instead of reporting dozens of phantom
    clearance errors."""
    slug = _seed(pcb)
    resp = pcb.get(id=slug, view="drc")
    assert "no realized copper yet" not in resp.body
    # NOT "(pads-only DRC ...)" — the array generator now writes its own
    # electrode-to-plaza-via stub as real `ctype='track'` copper at
    # generation time (`generators.py`'s own "the generator's own via is
    # a copper row now" note), so `_seed` alone (no `op='route'`) already
    # leaves `pcb_copper` non-empty. Pre-existing, independent of
    # gr341516 — noticed while updating this test for it, not caused by
    # it (this fixture is `@pytest.mark.slow` and evidently hadn't been
    # run in a while).
    assert "(pads-only DRC — no routed copper yet)" not in resp.body
    m = re.search(r"(\d+) error\(s\), (\d+) warn\(s\)", resp.body)
    assert m is not None

    ref = store.get_ref(kind="pcb", id=slug)
    assert ref is not None
    run_id, _findings = store.pcb_drc_findings_latest(ref.id)
    assert run_id is not None, "the DRC run must be recorded — 'no rows' is not clean"

    # The array ALONE, through the IR/DRC pad path (`_drc_pads` ->
    # `realize.pads_for_ir`), against the same capability + electrode-gap
    # net class the view itself resolves. Isolating the array is what
    # makes this assertion meaningful: the sink's channel pins sit on the
    # SAME nets as the electrodes they drive, so a net-name filter over
    # the whole board's findings could never tell an array-internal pair
    # from an array-vs-sink one.
    from precis.pcb import drc as pcb_drc
    from precis.pcb.capabilities import capability_for
    from precis.pcb.rules import resolve_net_rules

    design = store.pcb_load(ref.id)
    layer_names = [str(layer["name"]) for layer in design["board"]["stackup"]]
    capability = capability_for(pcb_drc.process_for_stackup(design["board"]["stackup"]))
    net_classes = design.get("net_classes") or {}
    net_rules = {
        str(n["name"]): resolve_net_rules(
            str(n.get("net_class") or ""),
            layer_is_outer=True,
            fab_caps=capability,
            overrides=net_classes.get(n.get("net_class") or ""),
            current_a=n.get("est_current_a"),
        )
        for n in design["nets"]
    }
    all_pads = pcb._drc_pads(ref.id, layer_names)
    array_pads = [p for p in all_pads if str(p.get("refdes")) == "ARR1"]
    assert len(array_pads) >= 50
    array_model = {
        "layers": layer_names,
        "copper": [],
        "pads": array_pads,
        "drills": [],
    }
    array_errors = [
        f
        for f in pcb_drc.check_clearance(array_model, capability, net_rules=net_rules)
        if f.severity == "error"
    ]
    detail = "\n".join(
        f"  {f.rule}: {f.where} :: {str(f.detail)[:140]}" for f in array_errors[:12]
    )
    assert not array_errors, (
        f"{len(array_errors)} array-INTERNAL clearance error(s) through the IR pad "
        "path — either a real geometry defect or a regression of the pad-source "
        f"agreement gripe 338983 closed:\n{detail}"
    )

    # gr341516: the array PLUS the bottom-side sink beneath it, both
    # through the same IR/DRC pad path — before the fix, this pair alone
    # produced dozens of phantom cross-layer clearance errors (the
    # documented side gap the module docstring used to describe); now
    # they are on correctly-opposite reported layers (F.Cu/B.Cu) and
    # never contend at all.
    sink_refdes = "ARR1_SINK_0"
    two_refdes_pads = [
        p for p in all_pads if str(p.get("refdes")) in ("ARR1", sink_refdes)
    ]
    assert any(p.get("refdes") == sink_refdes for p in two_refdes_pads)
    sink_layers = {
        str(p.get("layer")) for p in two_refdes_pads if p.get("refdes") == sink_refdes
    }
    array_layers = {
        str(p.get("layer")) for p in two_refdes_pads if p.get("refdes") == "ARR1"
    }
    assert sink_layers and array_layers and sink_layers.isdisjoint(array_layers), (
        f"expected the sink's pads on a different reported layer than the "
        f"array's: sink={sink_layers} array={array_layers}"
    )
    combined_model = {
        "layers": layer_names,
        "copper": [],
        "pads": two_refdes_pads,
        "drills": [],
    }
    combined_errors = [
        f
        for f in pcb_drc.check_clearance(
            combined_model, capability, net_rules=net_rules
        )
        if f.severity == "error"
    ]
    combined_detail = "\n".join(
        f"  {f.rule}: {f.where} :: {str(f.detail)[:140]}" for f in combined_errors[:12]
    )
    assert not combined_errors, (
        f"{len(combined_errors)} array-vs-sink clearance error(s) survived "
        f"gr341516:\n{combined_detail}"
    )


def test_dogfood_drc_pads_sit_where_the_exported_copper_does(pcb):
    """gripe 338983's regression test, at board level: the pad set DRC /
    the router / connectivity measure (``realize.pads_for_ir``, IR-sourced)
    and the pad set the gerber writer flashes (``padplace.board_pads``,
    footprint-sourced) must describe the SAME copper.

    They did not. ``pads_for_ir`` took each pad's SIZE/shape/poly from the
    real footprint but its POSITION from ``ir.pin_dx``/``pin_dy`` — a
    generic label-keyed land pattern — so every electrode of this board's
    72-pin custom array was checked as a correctly-shaped polygon anchored
    at a wrong place (measured: ~10mm off), while the exported gerber drew
    it correctly. Two pad sources, one board, silently disagreeing: this
    package's recurring defect, the same shape the ``pads_for_ir``
    docstring records for the size half of it.
    """
    from precis.pcb import padplace

    slug = _seed(pcb)
    ref = pcb.store.get_ref(kind="pcb", id=slug)
    assert ref is not None
    design = pcb.store.pcb_load(ref.id)
    graph = pcb.store.pcb_graph(ref.id)
    layer_names = [str(layer["name"]) for layer in design["board"]["stackup"]]

    drc_pads = pcb._drc_pads(ref.id, layer_names)
    exported, _drills = padplace.board_pads(
        design["instances"],
        pcb.store.pcb_footprints_for(ref.id),
        layers=layer_names,
        pin_to_net={
            (m["refdes"], m["pin"]): net["name"]
            for net in graph["nets"]
            for m in net["members"]
        },
        local_footprints=pcb.store.pcb_local_footprints_for(ref.id),
    )

    # The array's own electrodes only: their footprint is the one with no
    # package-family label for the synthesis to approximate, so this is
    # where the defect was metres-wide rather than tenths of a millimetre.
    drc_by_pin = {str(p["pin"]): p for p in drc_pads if str(p.get("refdes")) == "ARR1"}
    assert len(drc_by_pin) >= 50, "expected the whole 8x8 field's pins"
    # The electrode BODY among each pin's several exported pads (body +
    # neck stub + drilled via all share one pin/net) is the largest
    # polygon — the same "first/biggest is the electrode" identification
    # `realize._real_pad_sizes` makes by iteration order.
    body_by_net: dict[str, list[list[float]]] = {}
    for pad in exported:
        poly = pad.get("poly")
        if not poly:
            continue
        net = str(pad.get("net") or "")
        area = (max(v[0] for v in poly) - min(v[0] for v in poly)) * (
            max(v[1] for v in poly) - min(v[1] for v in poly)
        )
        prev = body_by_net.get(net)
        if prev is None or area > (
            (max(v[0] for v in prev) - min(v[0] for v in prev))
            * (max(v[1] for v in prev) - min(v[1] for v in prev))
        ):
            body_by_net[net] = [[float(v[0]), float(v[1])] for v in poly]

    checked = 0
    for pin, pad in drc_by_pin.items():
        body = body_by_net.get(f"ARR1_{pin}")
        if body is None:
            continue  # an unusable pad (no plaza escape) carries no net
        cx = (max(v[0] for v in body) + min(v[0] for v in body)) / 2.0
        cy = (max(v[1] for v in body) + min(v[1] for v in body)) / 2.0
        assert float(pad["x"]) == pytest.approx(cx, abs=0.01), (
            f"{pin}: DRC pad at ({pad['x']}, {pad['y']}), exported electrode "
            f"body centred at ({cx}, {cy}) — the two pad sources disagree"
        )
        assert float(pad["y"]) == pytest.approx(cy, abs=0.01)
        assert len(pad["poly"]) == len(body)
        checked += 1
    assert checked >= 50


def test_dogfood_capability_map_svg_renders(pcb):
    slug = _seed(pcb)
    resp = pcb.get(id=slug, view="capability")
    assert "<svg" in resp.body
    assert "R0C2" in resp.body or "RESV" in resp.body  # some pin label present


def _drain_one_job(store, parent_id: int) -> None:
    """Drain the job most recently enqueued under ``parent_id`` — same
    helper (and same "tolerate an unrelated stray row" reasoning, gr295496)
    as ``tests/test_pcb_reference_end_to_end.py``'s own."""
    from precis.workers.executors._common import TERMINAL, current_status
    from precis.workers.executors.job_inproc import run_job_inproc_pass

    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT ref_id FROM refs WHERE kind = 'job' AND parent_id = %s "
            "ORDER BY ref_id DESC LIMIT 1",
            (parent_id,),
        ).fetchone()
    assert row is not None, f"no job was ever queued for parent_id={parent_id}"
    job_ref_id = row[0]
    for _ in range(25):
        with store.pool.connection() as conn:
            status = current_status(conn, job_ref_id)
        if status in TERMINAL:
            assert status == "succeeded", (
                f"job {job_ref_id} failed to drain cleanly: status={status!r}"
            )
            return
        result = run_job_inproc_pass(store, limit=1)
        assert result["claimed"] == 1, f"expected a queued job, got {result}"
    raise AssertionError(f"job {job_ref_id} never reached a terminal status")


@pytest.mark.slow
def test_dogfood_route_op_routes_real_geometry_and_reports_the_escape_gap(pcb, store):
    """``op='route'`` driven for real on this board — the first time the
    spec's "B.Cu-only escape needs no new routing code" claim
    (:mod:`precis.pcb.generators`'s module docstring) could be TESTED at
    all: before gripe 338983 was fixed the router was connecting
    SYNTHESIZED land-pattern coordinates, so any routed track proved
    nothing about the real board.

    **Two results, both pinned here.**

    1. Routing now runs on REAL geometry: every routed track ends on its
       own net's real pad position (the gripe-338983 property in its
       routing form — the assertion that would have failed loudly before
       the fix, since the board's pads and the router's idea of them were
       ~10mm apart).
    2. **The escape claim now holds**: electrode escapes route through
       the authored plaza fabric (the plaza VIA that drops the electrode
       to B.Cu). Two engine fixes got it there, and this assertion was
       their inverse until the second landed: island terminals from fixed
       copper (docs/backlog/pcb-pre-place-route-blocks.md) made the via a
       terminal the router can see, and gripe 346962 stopped the maze
       claiming every pad as its ENCLOSING DISC — at a pitch narrower
       than the disc every F.Cu cell was walled off by a neighbour, so
       zero escapes could route even with the via visible. The escapes
       that still fail lose a congestion race (gripe 347037) and must
       keep a recorded reason.

    Both halves guard the same failure mode: a board that reports
    success while its electrodes connect to nothing. (1) catches copper
    that ends nowhere real; (2) catches the wall coming back (zero
    escapes) and a failure with no reason."""
    import collections

    slug = _seed(pcb)
    ref = store.get_ref(kind="pcb", id=slug)
    assert ref is not None

    resp = pcb.put(id=slug, args={"op": "route", "seed": 1})
    assert "enqueued" in resp.body
    # gr346951: the reply names the session's build and says the job runs
    # on the cluster's, so a stale-cluster re-route can't be misread.
    assert "Runs on the cluster worker's code" in resp.body
    assert "`ran_on:` line" in resp.body
    _drain_one_job(store, ref.id)

    design = store.pcb_load(ref.id)
    copper = store.pcb_copper_list(int(design["board"]["board_id"]))
    tracks = [c for c in copper if c.get("ctype") == "track"]
    status_by_net = {
        str(r["name"]): str(r["status"]) for r in store.pcb_route_status(ref.id)
    }
    routes = store.pcb_routes_get(ref.id)
    diag = (
        f"{len(tracks)} track(s); status "
        f"{dict(collections.Counter(status_by_net.values()))}; track nets "
        f"{sorted({str(t.get('net')) for t in tracks})[:12]}"
    )
    assert tracks, f"op='route' drew no copper at all — {diag}"

    # (1) Every routed track ends on its own net's REAL pad geometry OR
    # its own net's authored fixed copper (a via centre / track endpoint
    # from `pcb_fixed_copper` — the plaza's own via/stub fabric). The
    # router legitimately terminates on that fabric now (island terminals,
    # docs/backlog/pcb-pre-place-route-blocks.md): a net whose escape IS
    # the authored plaza via ends there by design, not by disagreement.
    # An endpoint near NEITHER is still the gripe-338983 failure.
    #
    # **Layer-aware, not (x, y)-only** (gr341516's sibling defect,
    # bottom-mounted-pad routing): a track ending at the RIGHT coordinate
    # on the WRONG copper layer is disconnected, not connected — exactly
    # what a layer-blind router that claims every pad on F.Cu regardless
    # of mount side produces for a bottom-mounted part (U_TEMP's I2C
    # pads). So each candidate target below carries the LAYER(S) it is
    # real copper on (a pad: its one `pads_for_ir` layer; a fixed/routed
    # via: every layer its span covers), and a track endpoint only counts
    # as reaching a target when their layer sets overlap — either the
    # track's OWN layer, or a layer its own capping via (this net's routed
    # via landing within the same match radius) extends it to.
    layer_names = [str(layer["name"]) for layer in design["board"]["stackup"]]

    def _span_layers(span: list[Any]) -> frozenset[str]:
        lo, hi = layer_names.index(str(span[0])), layer_names.index(str(span[1]))
        lo, hi = min(lo, hi), max(lo, hi)
        return frozenset(layer_names[lo : hi + 1])

    pads_by_net: dict[str, list[tuple[float, float, frozenset[str]]]] = {}
    for pad in pcb._drc_pads(ref.id, layer_names):
        net = str(pad.get("net") or "")
        if net:
            pads_by_net.setdefault(net, []).append(
                (float(pad["x"]), float(pad["y"]), frozenset({str(pad["layer"])}))
            )
    fixed_by_net: dict[str, list[tuple[float, float, frozenset[str]]]] = {}
    for row in store.pcb_fixed_copper_list(int(design["board"]["board_id"])):
        net = str(row.get("net") or "")
        if not net:
            continue
        pts: list[tuple[float, float, frozenset[str]]] = []
        if row.get("ctype") == "via" and row.get("x") is not None:
            span = row.get("span")
            via_layers = _span_layers(span) if span else frozenset(layer_names)
            pts.append((float(row["x"]), float(row["y"]), via_layers))
        elif row.get("ctype") == "track":
            track_layers = frozenset({str(row.get("layer"))})
            for seg in row.get("segments") or []:
                pts.append(
                    (float(seg["start"][0]), float(seg["start"][1]), track_layers)
                )
                pts.append((float(seg["end"][0]), float(seg["end"][1]), track_layers))
        fixed_by_net.setdefault(net, []).extend(pts)
    vias_by_net: dict[str, list[tuple[float, float, frozenset[str]]]] = {}
    for c in copper:
        if c.get("ctype") != "via":
            continue
        net = str(c.get("net") or "")
        span = c.get("span")
        if not net or not span:
            continue
        vias_by_net.setdefault(net, []).append(
            (float(c["x"]), float(c["y"]), _span_layers(span))
        )
    for track in tracks:
        if track.get("is_dogbone"):
            continue  # a plane fan-out stub ends at its drop via, not a pad
        segs = track["segments"]
        net = str(track["net"])
        track_layer = str(track.get("layer"))
        targets = pads_by_net.get(net, []) + fixed_by_net.get(net, [])
        assert targets, f"routed net {track['net']} has no pads at all — {diag}"
        for end in (
            (float(segs[0]["start"][0]), float(segs[0]["start"][1])),
            (float(segs[-1]["end"][0]), float(segs[-1]["end"][1])),
        ):
            # Every layer this endpoint's copper actually reaches: its own
            # drawn layer, widened by any of this net's ROUTED vias
            # landing within the same match radius (a track capped by a
            # via reaches every layer that via's barrel spans, not just
            # the layer it was drawn on).
            own_vias_here = [
                (vx, vy)
                for vx, vy, _ in vias_by_net.get(net, [])
                if math.hypot(end[0] - vx, end[1] - vy) < 1.0
            ]
            reachable = {track_layer} | {
                layer
                for vx, vy, vlayers in vias_by_net.get(net, [])
                if (vx, vy) in own_vias_here
                for layer in vlayers
            }
            # A track ending on one of THIS NET's own routed vias is
            # itself real, placed copper -- sufficient proof of
            # connectivity on its own, with no pad match required. This
            # is the plane-stitching jumper's own shape (module docstring
            # / `_stitch_plane_fragments`/`_try_plane_jumper`): a jumper's
            # two ends are via centres dropped into two GND pour
            # FRAGMENTS, never a pin's pad, so "ends near a pad" is simply
            # the wrong question for it. Before this fix's own change to
            # per-instance pad layers, GND's pour never fragmented on this
            # fixture (every claim sat on one shared, if wrong, layer), so
            # this path never fired here; the fix's more accurate
            # per-side pad claims now legitimately split GND's pour and
            # this pass legitimately bridges it.
            assert own_vias_here or any(
                math.hypot(end[0] - tx, end[1] - ty) < 1.0 and (reachable & tlayers)
                for tx, ty, tlayers in targets
            ), (
                f"{track['net']}: track end {end} on {track_layer} (reachable: "
                f"{sorted(reachable)}) is nowhere near any of its own pads, "
                f"fixed copper, or own routed vias ON A LAYER ITS COPPER "
                f"ACTUALLY REACHES {targets} — the router and the board "
                "disagree about where this net's copper is (gripe 338983's "
                "signature / layer-blind pad claim)"
            )

    # (2) The escape gap is CLOSED: electrode escapes route through the
    # authored plaza fabric. This assertion was its inverse until gripe
    # 346962 — island terminals made the plaza via a real terminal, but
    # every F.Cu cell was still walled off by the neighbours' enclosing
    # pad DISCS (wider than the pitch by construction), so zero escapes
    # could route and the test pinned that visibly. True-shape pad claims
    # lifted the wall; the floor here is half of what seed=1 realizes so
    # a tuning wobble can't redden it, while a regression to the disc
    # claim (0 realized) or a silently-'realized' escape that assertion
    # (1) would catch both still fail loudly. The escapes that still fail
    # lose a congestion race (gripe 347037) and must keep a recorded
    # reason — a failure with no reason is the silent board this fixture
    # exists to prevent.
    escape_nets = [n for n in status_by_net if n.startswith("ARR1_R")]
    assert len(escape_nets) >= 50
    realized_escapes = [n for n in escape_nets if status_by_net[n] == "realized"]
    # Rulings 2026-09-19 item 7 (net-class `layers`) LOWERED this floor
    # from its pre-item-7 value: the escape nets are genuinely LOCKED to
    # B.Cu-only (module docstring's "no crossovers"), so a net that used
    # to route by borrowing a layer change through the field can no
    # longer do that -- a real, expected drop in raw count, not a
    # regression this fixture should paper over.
    #
    # Items 3/10/11 (250V drive, 2.25mm pitch, B.Cu breakout stubs)
    # RAISE it again: measured 13/54 escape nets realized at seed=1 on
    # this fixture with the declared voltage/pitch and the breakout
    # stubs both in place (up from the pre-breakout, pre-voltage floor of
    # 3, out of 57 nets -- the declared voltage also moved the fixture's
    # own net count slightly). Floor set to half the observed count, same
    # "a tuning wobble can't redden it" margin every earlier floor here
    # used.
    assert len(realized_escapes) >= 6, (
        "electrode escapes no longer route through the plaza fabric — the "
        f"gripe-346962 wall (enclosing pad discs) is back? {diag}"
    )
    for net in escape_nets:
        if status_by_net[net] == "realized":
            continue
        problems = ((routes.get(net) or {}).get("fail") or {}).get("problems") or []
        assert problems, f"escape net {net} failed to route with NO recorded reason"

    # (3) Rulings 2026-09-19 item 7: "the escape is plaza via -> B.Cu
    # track -> sink pad, nothing else" -- every escape net's ROUTER-drawn
    # copper (`fixed` is absent/False; the authored plaza via/neck stub
    # is `fixed: True` and does not count, module docstring's own
    # distinction) is B.Cu-only, with no router-added via at all (a via
    # would mean the router itself crossed off B.Cu somewhere).
    router_tracks = [t for t in tracks if not t.get("fixed")]
    escape_track_layers = {
        t["layer"] for t in router_tracks if t["net"] in set(realized_escapes)
    }
    assert escape_track_layers <= {"B.Cu"}, (
        "a realized escape net's router-drawn copper touched a layer "
        f"other than B.Cu: {escape_track_layers}"
    )
    router_via_escape_nets = {
        c["net"] for c in copper if c.get("ctype") == "via" and not c.get("fixed")
    } & set(escape_nets)
    assert not router_via_escape_nets, (
        f"the router placed its own via on escape net(s) "
        f"{router_via_escape_nets} — the B.Cu-only lock should make that "
        "impossible"
    )

    # Post-route DRC is the FULL check now (routed copper exists), not the
    # pads-only scope an un-routed board reports.
    drc = pcb.get(id=slug, view="drc")
    assert "(pads-only DRC — no routed copper yet)" not in drc.body
    assert "error(s)" in drc.body


def test_dogfood_gerber_export_zip_loads(pcb, tmp_path):
    slug = _seed(pcb)
    resp = pcb.get(id=slug, view="gerber", args={"dir": str(tmp_path)})
    assert "SynthesizedPadError" not in resp.body
    zpath = tmp_path / f"{slug}-fab.zip"
    assert zpath.exists()
    with zipfile.ZipFile(zpath) as zf:
        bad = zf.testzip()
        assert bad is None
        names = set(zf.namelist())
        assert f"{slug}-F_Cu.gbr" in names
        assert f"{slug}-B_Cu.gbr" in names
        f_cu = zf.read(f"{slug}-F_Cu.gbr").decode("utf-8")
        assert "G36*" in f_cu and "G37*" in f_cu  # the polygon electrodes
        # Rulings 2026-09-19 item 11: every plaza via's B.Cu breakout stub
        # is a real DRAWN track (D02*/D01*), not just the via's own flash
        # (D03*) already on this layer -- distinguishes "the stub actually
        # exported" from "the via happens to span this layer too".
        b_cu = zf.read(f"{slug}-B_Cu.gbr").decode("utf-8")
        assert "D02*" in b_cu and "D01*" in b_cu, (
            "no drawn (D01*) track on B.Cu -- the plaza via breakout stubs "
            "did not export"
        )


def test_dogfood_fab_svg_render_is_well_formed(pcb, tmp_path):
    """Not a substitute for the "found by looking" DoD step (module
    docstring / docs/backlog/pcb-ewod-multitile.md acceptance criterion
    2) -- that is a one-time manual render-and-look review, not something
    a repeatable test should re-do on every CI run (writing an SVG
    artifact into the tracked tree on every pass would dirty it
    needlessly). This just pins that the view keeps producing a
    non-trivial, well-formed render as the fixture evolves."""
    slug = _seed(pcb)
    resp = pcb.get(id=slug, view="svg", args={"level": "fab"})
    assert "<svg" in resp.body
    assert resp.body.count("<svg") == 1
    assert len(resp.body) > 1000

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


def _qfp_ring_footprint(
    pin_names: list[str],
    *,
    sides: tuple[int, int, int, int] = (16, 24, 16, 24),
    pitch: float = 0.8,
    x_half: float = 8.5,
    y_half: float = 11.5,
    pad_w: float = 0.35,
    pad_h: float = 0.35,
) -> dict[str, Any]:
    """A RING-shaped (QFP) stand-in, shaped like the real part — sibling
    of :func:`_grid_footprint` and a port of
    ``tests/test_pcb_island_terminal_polygon.py``'s ``_ring_footprint``
    (which is per-side-symmetric; this one takes the four side counts
    separately, because the real package is not square).

    **Why this replaced the grid stand-in for the sink** (2026-09-26): a
    grid puts pads in the package INTERIOR, and an interior pad at 1.0mm
    pitch with 0.5mm pads leaves 0.5mm between pad edges — less than the
    0.15mm-per-side the maze grid dilates every pad by, so interior pads
    are unreachable *by construction*. Prod's real C639448 is a PQFP-80:
    80 pads, **ZERO interior**, 17x23mm (queried against prod). The grid
    fixture therefore manufactured a routing wall the real board does not
    have, and every escape-yield number measured on it was measured
    against that artifact. ``_ring_footprint``'s own docstring had
    already recorded the same point for gr339236.

    Defaults match the real part's measured extent: 16/24/16/24 pads at
    0.8mm on a 17x23mm ring = 80 pads. ``pin_names`` shorter than the pad
    count leaves the remainder named ``NC<n>`` — which makes this the
    free test vehicle for gr451276 (a part with MORE footprint pads than
    the netlist declares). Pads are square, like both stand-ins before
    it: this is a fixture for pad GEOMETRY and package TOPOLOGY, never a
    datasheet-accurate land pattern.
    """
    n_bottom, n_right, n_top, n_left = sides

    def _walk(n: int) -> list[float]:
        span = (n - 1) * pitch
        return [-span / 2.0 + i * pitch for i in range(n)]

    positions: list[tuple[float, float]] = []
    for x in _walk(n_bottom):  # bottom edge, left -> right
        positions.append((x, -y_half))
    for y in _walk(n_right):  # right edge, bottom -> top
        positions.append((x_half, y))
    for x in reversed(_walk(n_top)):  # top edge, right -> left
        positions.append((x, y_half))
    for y in reversed(_walk(n_left)):  # left edge, top -> bottom
        positions.append((-x_half, y))

    pads = []
    pin_map = {}
    for i, (x, y) in enumerate(positions):
        number = str(i + 1)
        name = pin_names[i] if i < len(pin_names) else f"NC{i - len(pin_names) + 1}"
        pads.append(
            {
                "number": number,
                "shape": "RECT",
                "x": x,
                "y": y,
                "w": pad_w,
                "h": pad_h,
                "rot": 0.0,
                "layer": "F.Cu",
                "drill": None,
            }
        )
        pin_map[number] = {"name": name, "tags": []}
    return {"pads": pads, "pin_map": pin_map}


@pytest.fixture
def pcb(store):
    return PcbHandler(hub=Hub(store=store))


def _design(escape_layers: list[str] | None = None) -> dict[str, Any]:
    """``escape_layers`` is DESIGN data the generator passes through (see
    ``generators.py``'s own comment on it): omitted, the generator derives
    "every signal layer the electrode field does not own", which on
    ``DEFAULT_STACKUP`` is ``["B.Cu"]``. Supply it together with an
    ``op='stackup'`` that actually makes those layers routable — the two
    have to agree, and neither implies the other."""
    design: dict[str, Any] = {
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
    if escape_layers is not None:
        design["generators"][0]["params"]["escape_layers"] = list(escape_layers)
    return design


def _seed(pcb, escape_layers: list[str] | None = None) -> str:
    design = _design(escape_layers)
    pcb.put(id="ewod-dogfood-1", args=design)
    # RING, not a grid: the real C639448 is a PQFP-80 with no interior
    # pads. See _qfp_ring_footprint's docstring for why a grid stand-in
    # invalidated every escape-yield number measured before 2026-09-26.
    pcb.store.part_footprint_put(_HV507_LCSC, _qfp_ring_footprint(_HV507_PINS))
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

    # gr451276 — the driver's real footprint declares pads the netlist
    # never names: `_qfp_ring_footprint` pads 69 pins out to an 80-pad
    # ring, leaving 11 `NC<n>` lands. The gerber writer flashes all 80, so
    # DRC and the router must see all 80 — copper routed through an
    # unnamed land is a real short, and before this fix `pads_for_ir`
    # walked `ir.pin_*` and could not see one.
    def _sink_pins(pads):
        return {
            str(p["pin"])
            for p in pads
            if str(p.get("refdes") or "").startswith("ARR1_SINK")
        }

    sink_exported = _sink_pins(exported)
    nc_lands = {pin for pin in sink_exported if pin.startswith("NC")}
    assert len(nc_lands) == 11, (
        f"the stand-in's 80-pad ring should leave 11 NC lands: {sorted(nc_lands)}"
    )
    assert nc_lands <= _sink_pins(drc_pads), (
        "NC lands the gerber flashes are missing from the DRC/router pad "
        f"set: {sorted(nc_lands - _sink_pins(drc_pads))}"
    )
    assert _sink_pins(drc_pads) == sink_exported, (
        "the two pad sources must describe the same 80 lands"
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

    # This net's OWN routed track copper, per layer — the third legal
    # thing an endpoint may land on. `OccupancyGrid.route`'s multi-source
    # start (``attach=True``, the default `_route_pass` uses) lets a later
    # connection of a net begin anywhere on copper that net already owns,
    # which draws a T-junction into an earlier track instead of a second
    # run back to the pad. That is real, placed, same-net copper and it is
    # LESS copper than the alternative; `connectivity.net_islands` models
    # a track as a capsule and reports the junction as one component.
    # Before this was modelled here, an attach-formed T read as "ends
    # nowhere real" and the assertion below fired on a correct board
    # (2026-09-27, GND seg 59 starting 0.000mm from GND's own earlier
    # B.Cu run, while `net_islands` said GND was one piece).
    runs_by_net: dict[
        str, list[tuple[int, str, float, tuple[float, float], tuple[float, float]]]
    ] = {}
    for k, t in enumerate(tracks):
        for seg in t["segments"]:
            runs_by_net.setdefault(str(t["net"]), []).append(
                (
                    k,
                    str(t.get("layer")),
                    float(t.get("width_mm") or 0.0),
                    (float(seg["start"][0]), float(seg["start"][1])),
                    (float(seg["end"][0]), float(seg["end"][1])),
                )
            )

    def _point_to_segment_mm(p, a, b) -> float:
        dx, dy = b[0] - a[0], b[1] - a[1]
        span2 = dx * dx + dy * dy
        t = 0.0 if span2 == 0.0 else ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / span2
        t = max(0.0, min(1.0, t))
        return math.hypot(p[0] - (a[0] + t * dx), p[1] - (a[1] + t * dy))

    def _track_ends(t):
        s = t["segments"]
        return [
            (float(s[0]["start"][0]), float(s[0]["start"][1])),
            (float(s[-1]["end"][0]), float(s[-1]["end"][1])),
        ]

    def _reachable_from(t, end) -> set[str]:
        """Every layer this endpoint's copper reaches: its own drawn
        layer, widened by any of this net's routed vias landing within
        the match radius (a track capped by a via reaches every layer
        that barrel spans)."""
        out = {str(t.get("layer"))}
        for vx, vy, vlayers in vias_by_net.get(str(t["net"]), []):
            if math.hypot(end[0] - vx, end[1] - vy) < 1.0:
                out |= vlayers
        return out

    # **Anchoring is TRANSITIVE, and has to be checked as such.** Letting
    # an endpoint sit on any same-net run would let two tracks that only
    # ever touch each other excuse one another and float free of every
    # pad — which is gripe 338983's signature, the exact thing this
    # assertion exists to catch. So a T-junction only counts when the run
    # it lands on is itself anchored: seeded from tracks with an end at a
    # real pad / fixed-copper point / this net's own routed via, then
    # propagated to fixpoint. A plane fan-out stub (`is_dogbone`) is
    # anchored by construction — it ends at its own drop via — which is
    # why it is exempt from the assertion but still a legal thing to
    # attach to.
    def _at_real_copper(t, end) -> bool:
        net_t = str(t["net"])
        if any(
            math.hypot(end[0] - vx, end[1] - vy) < 1.0
            for vx, vy, _ in vias_by_net.get(net_t, [])
        ):
            return True
        reach = _reachable_from(t, end)
        return any(
            math.hypot(end[0] - tx, end[1] - ty) < 1.0 and (reach & tlayers)
            for tx, ty, tlayers in pads_by_net.get(net_t, [])
            + fixed_by_net.get(net_t, [])
        )

    anchored = {
        k
        for k, t in enumerate(tracks)
        if t.get("is_dogbone") or any(_at_real_copper(t, e) for e in _track_ends(t))
    }
    while True:
        grew = False
        for k, t in enumerate(tracks):
            if k in anchored:
                continue
            width_k = float(t.get("width_mm") or 0.0)
            for end in _track_ends(t):
                reach = _reachable_from(t, end)
                if any(
                    other in anchored
                    and other != k
                    and run_layer in reach
                    and _point_to_segment_mm(end, a, b) < (run_w + width_k) / 2.0
                    for other, run_layer, run_w, a, b in runs_by_net.get(
                        str(t["net"]), []
                    )
                ):
                    anchored.add(k)
                    grew = True
                    break
        if not grew:
            break

    for track_index, track in enumerate(tracks):
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
            # An attach-formed T-junction: this end sits ON another of
            # this net's own routed runs, on a layer its copper reaches,
            # and that run is itself ANCHORED (see the fixpoint above —
            # two tracks touching only each other excuse nothing).
            # The tolerance is the two runs' own COPPER — half of each
            # width, so the test is "these two bodies overlap", not a
            # tuned epsilon. It has to be that rather than 0: the stored
            # polyline is FILLETED (`_tracks_from_path`'s own corner
            # taut-up), so an anchor the router placed exactly on the raw
            # path sits a few hundredths off the rounded corner that
            # actually ships. Still an order of magnitude tighter than
            # the 1.0mm pad radius above, and it cannot excuse an
            # endpoint in bare board.
            track_w = float(track.get("width_mm") or 0.0)
            on_own_run = any(
                other != track_index
                and other in anchored
                and run_layer in reachable
                and _point_to_segment_mm(end, a, b) < (run_w + track_w) / 2.0
                for other, run_layer, run_w, a, b in runs_by_net.get(net, [])
            )
            assert (
                own_vias_here
                or on_own_run
                or any(
                    math.hypot(end[0] - tx, end[1] - ty) < 1.0 and (reachable & tlayers)
                    for tx, ty, tlayers in targets
                )
            ), (
                f"{track['net']}: track end {end} on {track_layer} (reachable: "
                f"{sorted(reachable)}) is nowhere near any of its own pads, "
                f"fixed copper, own routed vias, or own routed track runs ON "
                f"A LAYER ITS COPPER ACTUALLY REACHES {targets} — the router "
                "and the board disagree about where this net's copper is "
                "(gripe 338983's signature / layer-blind pad claim)"
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
    #
    # Later correction, MEASURED on this fixture at seed=1: swapping the
    # grid-shaped HV507 stand-in for a real peripheral ring
    # (`_qfp_ring_footprint`) took it to 35/54. A half-of-observed floor
    # could not tell 35 from 10, so it noticed neither that move nor a
    # regression; tightened here to a real number with a tuning margin.
    #
    # LOWERED 35 -> 28 on 2026-09-27 by gr451276's router half
    # (`realize._unclaimed_pad_claims`), and the drop is the POINT, not a
    # regression: six escape nets used to "realize" by drawing B.Cu
    # straight across an unclaimed land at 0.000mm clearance — 24 DRC
    # clearance ERRORS of the form `track[ARR1_RxCy] <-> pad[]`, all of
    # which that change takes to zero. An escape that shorts a land the
    # fab flashes was never realized; it was reported as realized.
    assert len(realized_escapes) >= 24, (
        "electrode escapes no longer route through the plaza fabric — the "
        f"gripe-346962 wall (enclosing pad discs) is back? {diag}"
    )
    for net in escape_nets:
        if status_by_net[net] == "realized":
            continue
        problems = ((routes.get(net) or {}).get("fail") or {}).get("problems") or []
        assert problems, f"escape net {net} failed to route with NO recorded reason"

    # (3) An escape never routes on the ELECTRODE layer. Rulings
    # 2026-09-19 item 7 wrote this as `"layers": ["B.Cu"]`; since
    # 2026-09-27 the generator derives it instead — every signal layer
    # the electrode field does not own — which on DEFAULT_STACKUP is the
    # same ["B.Cu"], because In1/In2 are PLANE layers and B.Cu is the
    # only routing layer this board has.
    #
    # The assertion is written against the RULE, not that one answer, so
    # it keeps holding when the stackup gains inner signal layers —
    # `test_dogfood_an_inner_signal_layer_nearly_closes_the_escape_gap`
    # below is that case, authored through `op='stackup'`, and it re-runs
    # this same F.Cu check. Whatever layers open up, F.Cu stays closed:
    # routing a trace across the electrode plane disturbs
    # the field the board exists to control — measured 2026-09-27, an
    # "any signal layer" escape class promptly did exactly that.
    router_tracks = [t for t in tracks if not t.get("fixed")]
    escape_track_layers = {
        t["layer"] for t in router_tracks if t["net"] in set(realized_escapes)
    }
    assert "F.Cu" not in escape_track_layers, (
        "an escape routed on F.Cu, which the electrode array owns: "
        f"{escape_track_layers}"
    )

    # Post-route DRC is the FULL check now (routed copper exists), not the
    # pads-only scope an un-routed board reports.
    drc = pcb.get(id=slug, view="drc")
    assert "(pads-only DRC — no routed copper yet)" not in drc.body
    assert "error(s)" in drc.body

    # (4) **No routed copper crosses an unclaimed land** (gr451276).
    # Reto's own words: "no wire can route thru it, even if it is nc". An
    # unclaimed footprint pad carries no net (`net: ""`) but is real
    # copper in the gerbers, so a track over one is a short the board
    # reports as a clean route. `realize._unclaimed_pad_rows` made those
    # lands VISIBLE to DRC; `_unclaimed_pad_claims` puts them in the
    # router's own occupancy grid so they are never crossed in the first
    # place. Measured on this fixture at seed=1: 24 such errors before,
    # 0 after. Asserted as a count of ZERO, not a ceiling — one is a
    # short.
    from precis.pcb import drc as pcb_drc
    from precis.pcb.capabilities import capability_for

    layer_names = [str(layer["name"]) for layer in design["board"]["stackup"]]
    model = {
        "layers": layer_names,
        "copper": [dict(c) for c in copper],
        "pads": pcb._drc_pads(ref.id, layer_names),
    }
    shorts = [
        f
        for f in pcb_drc.check_clearance(
            model,
            capability_for(pcb_drc.process_for_stackup(design["board"]["stackup"])),
        )
        if f.severity == "error" and "pad[]" in f.where
    ]
    assert not shorts, (
        f"{len(shorts)} routed track(s) cross an unclaimed (net-less) "
        f"footprint land: {[f.where for f in shorts[:6]]} — the router's "
        "grid is not claiming them (realize._unclaimed_pad_claims)"
    )


@pytest.mark.slow
def test_dogfood_an_inner_signal_layer_nearly_closes_the_escape_gap(pcb, store):
    """**The escape gap was a STACKUP problem, not a router problem.**

    Same board, same seed, same router as
    ``test_dogfood_route_op_routes_real_geometry_and_reports_the_escape_
    gap`` above — the only difference is two lines of DESIGN data:
    In2.Cu declared ``signal`` via ``op='stackup'``, and ``escape_layers``
    widened to match. Measured 2026-09-27:

    | | B.Cu only | + In2.Cu |
    | --- | --- | --- |
    | escapes realized | 28 / 54 | **50 / 54** |
    | DRC clearance errors | 0 | **0** |
    | nets whose copper is in >1 island | 26 | **4** |

    That beats the 46/54 the backlog had recorded as the measured
    ceiling, and unlike that arm this one is LEGAL: it opens an inner
    layer, not F.Cu, so the electrode field is never crossed (asserted
    below). Every number before this was measured on a board that had
    exactly one routing layer because ``DEFAULT_STACKUP`` said so and
    nothing could say otherwise — see
    docs/backlog/pcb-escape-and-driver-chain.md, "Blocked on".

    This is the acceptance test for ``put(args={'op':'stackup'})``: the
    engine's job is to let an LLM fulfil an arbitrary text request for a
    board, and "give me two routing layers and keep the ground plane" was
    a request it could not express.
    """
    slug = _seed(pcb, escape_layers=["B.Cu", "In2.Cu"])
    ref = store.get_ref(kind="pcb", id=slug)
    assert ref is not None
    pcb.put(
        id=slug,
        args={
            "op": "stackup",
            "layers": [
                {"name": "F.Cu", "role": "signal"},
                {"name": "In1.Cu", "role": "plane"},
                {"name": "In2.Cu", "role": "signal"},
                {"name": "B.Cu", "role": "signal"},
            ],
        },
    )
    pcb.put(id=slug, args={"op": "route", "seed": 1})
    _drain_one_job(store, ref.id)

    design = store.pcb_load(ref.id)
    copper = store.pcb_copper_list(int(design["board"]["board_id"]))
    status_by_net = {
        str(r["name"]): str(r["status"]) for r in store.pcb_route_status(ref.id)
    }
    # `ARR1_R<row>C<col>` — the electrode nets only, the same set the
    # route test above counts. A bare `ARR1_` prefix also catches the top
    # plate and the power rails, which route on F.Cu by design and would
    # make the electrode-layer assertion below vacuous.
    escape_nets = [n for n in status_by_net if n.startswith("ARR1_R")]
    realized = [n for n in escape_nets if status_by_net[n] == "realized"]
    diag = f"{len(realized)}/{len(escape_nets)} escapes realized"

    # A REAL number, re-baselined on the behaviour change (this item's own
    # acceptance criterion). Measured 50/54; floored at 46 rather than
    # pinned at 50, because the router is seeded but its rip-up ORDERING is
    # not a contract. The thing worth catching is a collapse back toward
    # the one-routing-layer 28, not a one- or two-escape tuning wobble.
    assert len(realized) >= 46, (
        f"opening In2.Cu no longer buys the escapes — {diag}; a drop toward "
        "28 means the second routing layer stopped being reachable"
    )

    # Still zero shorts. Yield alone could not tell "routed" from "routed
    # through a land" — that is how 35/54 stood for a week with 24 shorts
    # under it (backlog item 2). A layer that buys yield by drawing over
    # copper is not a win.
    from precis.pcb import drc as pcb_drc
    from precis.pcb.capabilities import capability_for

    layer_names = [str(layer["name"]) for layer in design["board"]["stackup"]]
    model = {
        "layers": layer_names,
        "copper": [dict(c) for c in copper],
        "pads": pcb._drc_pads(ref.id, layer_names),
    }
    errors = [
        f
        for f in pcb_drc.check_clearance(
            model,
            capability_for(pcb_drc.process_for_stackup(design["board"]["stackup"])),
        )
        if f.severity == "error"
    ]
    assert not errors, (
        f"{len(errors)} clearance ERROR(s) with In2.Cu open: "
        f"{[f.where for f in errors[:6]]} — {diag}"
    )

    # F.Cu stays closed however many layers open up. The electrode field
    # owns it; a trace across it disturbs the field the board exists to
    # control.
    escape_layers_used = {
        str(t["layer"])
        for t in copper
        if t.get("ctype") == "track"
        and not t.get("fixed")
        and str(t.get("net")) in set(realized)
    }
    assert "F.Cu" not in escape_layers_used, (
        f"an escape routed on the electrode layer: {escape_layers_used}"
    )
    assert "In2.Cu" in escape_layers_used, (
        "no escape actually used the newly-opened layer, so this test is "
        f"not measuring what it claims: {escape_layers_used}"
    )


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
        # Rulings 2026-09-19 item 11 gave every plaza via a B.Cu breakout
        # stub, a real DRAWN track (D02*/D01*) on that layer distinct from
        # the via's own flash (D03*); Reto removed that row 2026-09-29
        # (module docstring's "B.Cu breakout stub" section), so this
        # generator's OWN fixed copper on B.Cu is via flashes only now --
        # any drawn B.Cu track comes from the router, not from here.
        b_cu = zf.read(f"{slug}-B_Cu.gbr").decode("utf-8")
        assert "D03*" in b_cu  # the plaza vias' own flash still exports


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

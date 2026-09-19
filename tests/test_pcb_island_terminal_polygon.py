"""gr339236 fix — :mod:`precis.pcb.connectivity`'s fixed-copper touch test
against a POLYGON pad (module docstring; docs/backlog/
pcb-pre-place-route-blocks.md's "Realize seam").

**The bug.** :func:`~precis.pcb.connectivity.fixed_copper_pin_terminals`
and :func:`~precis.pcb.connectivity.connected_pin_pairs` decided whether a
pin's authored fixed copper (a stub track + plaza via) touches the pin's
pad by approximating the pad as its INSCRIBED circle — correct for every
circle/rect/obround pad, wrong for the EWOD electrode's square ``poly``
pad, whose authored stub starts at the pad's own CORNER (further from
centre than the inscribed radius by construction). The touch was missed,
the plaza via was never offered to the router as an island terminal, and
every escape net fell back to a pad-to-pad search that cannot reach
through the array's own packed neighbours (``no_path``) — 59/62 nets on
prod ``ewod-dogfood-2``.

**Two levels of coverage, matching the module's own two functions:**

1. :func:`test_polygon_pad_corner_is_touched_by_a_stub_the_inscribed_circle_misses`
   — a hand-built, minimal fixture reproducing the exact prod geometry
   (2.02mm square pad, corner-anchored stub) directly against
   :mod:`precis.pcb.connectivity`, with no DB and no generator. Proves
   the fixture actually bites by monkeypatching ``_pad_poly`` back to
   always-``None`` (the OLD behaviour) and checking the touch is then
   missed.
2. :func:`test_ring_sink_route_op_realizes_more_escape_nets_with_polygon_touch`
   — the real :func:`precis.pcb.generators.expand` ``ewod_pad_array``
   generator, run end-to-end through ``op='route'``, against a
   NON-grid ring sink footprint (HVOUT1..HVOUT64/DIOA/DIOB/VPP/VDD/GND —
   the real prod board's own pin naming) instead of
   ``tests/test_pcb_ewod_dogfood.py``'s grid-laid-out stand-in, so the
   sink's OWN pad positions cannot accidentally make every net trivial.
   Same before/after comparison via the ``_pad_poly`` monkeypatch,
   through the actual router this time.
"""

from __future__ import annotations

import collections
import math
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.handlers.pcb import PcbHandler
from precis.pcb import connectivity as pcb_connectivity
from tests.test_pcb_ewod_dogfood import _drain_one_job

pytestmark = pytest.mark.db


# ── 1. connectivity-level: the corner-of-a-square-pad fixture ───────────


def test_polygon_pad_corner_is_touched_by_a_stub_the_inscribed_circle_misses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Real prod net ARR1_R0C2's own numbers (gr339236's repro): a 2.02mm
    # square pad centred at (-3, -7) -- inscribed radius 1.01mm -- whose
    # authored stub starts at its own corner region (-3.95, -6.05), 1.34mm
    # from centre. A second, ordinary circular pad on the same net stands
    # in for wherever the rest of the net's copper goes (a driver pin),
    # reached through the via by a second stub -- so the fixture exercises
    # both functions this module's fix touches: the pin's OWN island
    # (`fixed_copper_pin_terminals`) and the PAIR the island bridges to
    # (`connected_pin_pairs`).
    cx, cy = -3.0, -7.0
    half = 1.01  # w = h = 2.02mm -> inscribed circle radius
    poly = [
        [cx - half, cy - half],
        [cx + half, cy - half],
        [cx + half, cy + half],
        [cx - half, cy + half],
    ]
    stub_start = (-3.95, -6.05)
    via_xy = (-3.95, -3.0)
    drv_xy = (10.0, -7.0)

    # Sanity: the fixture only "bites" if the inscribed circle really
    # would have missed this touch -- otherwise this test would pass for
    # a reason that has nothing to do with the fix.
    assert math.hypot(stub_start[0] - cx, stub_start[1] - cy) > half

    model: dict[str, Any] = {
        "layers": ["F.Cu", "B.Cu"],
        "pads": [
            {
                "net": "ARR1_R0C2",
                "refdes": "ARR1",
                "pin": "R0C2",
                "shape": "polygon",
                "x": cx,
                "y": cy,
                "w": 2 * half,
                "h": 2 * half,
                "layer": "F.Cu",
                "poly": poly,
            },
            {
                "net": "ARR1_R0C2",
                "refdes": "DRV1",
                "pin": "1",
                "shape": "circle",
                "x": drv_xy[0],
                "y": drv_xy[1],
                "w": 0.4,
                "h": 0.4,
                "layer": "F.Cu",
            },
        ],
        "copper": [
            {
                "ctype": "track",
                "net": "ARR1_R0C2",
                "layer": "F.Cu",
                "width_mm": 0.15,
                "segments": [
                    {"shape": "line", "start": list(stub_start), "end": list(via_xy)}
                ],
            },
            {
                "ctype": "via",
                "net": "ARR1_R0C2",
                "x": via_xy[0],
                "y": via_xy[1],
                "dia_mm": 0.45,
                "drill_mm": 0.2,
                "span": ["F.Cu", "B.Cu"],
            },
            {
                "ctype": "track",
                "net": "ARR1_R0C2",
                "layer": "F.Cu",
                "width_mm": 0.15,
                "segments": [
                    {"shape": "line", "start": list(via_xy), "end": list(drv_xy)}
                ],
            },
        ],
    }

    key = ("ARR1", "R0C2")

    # (a) the fixed pin's own island offers the via as a terminal.
    terminals = pcb_connectivity.fixed_copper_pin_terminals(model)
    assert key in terminals
    assert via_xy in {t.point for t in terminals[key]}

    # (b) the pin is recognised as already bridged to the driver pin.
    pairs = pcb_connectivity.connected_pin_pairs(model)
    assert frozenset({("ARR1", "R0C2"), ("DRV1", "1")}) in pairs

    # (c) with the OLD inscribed-circle-only touch test (`_pad_poly`
    # forced to report no polygon, same as every pad had before this
    # fix), the SAME model misses both -- the fixture bites.
    monkeypatch.setattr(pcb_connectivity, "_pad_poly", lambda pad: None)
    old_terminals = pcb_connectivity.fixed_copper_pin_terminals(model)
    assert key not in old_terminals
    old_pairs = pcb_connectivity.connected_pin_pairs(model)
    assert frozenset({("ARR1", "R0C2"), ("DRV1", "1")}) not in old_pairs


# ── 2. end-to-end: ewod_pad_array + a real ring-package sink footprint ──

_RING_LCSC = (
    "C639448"  # placeholder C-number, never fetched (footprint authored directly)
)
_RING_CHANNELS = [f"HVOUT{i + 1}" for i in range(64)]
_RING_PINS = [*_RING_CHANNELS, "DIOA", "DIOB", "VPP", "VDD", "GND"]  # 69 named pins


def _ring_footprint(
    pin_names: list[str],
    *,
    per_side: int,
    x_half: float,
    y_half: float,
    pitch: float = 0.65,
    pad_w: float = 0.35,
    pad_h: float = 0.35,
) -> dict[str, Any]:
    """A synthetic ring-package footprint (QFP-shaped, NOT
    ``tests/test_pcb_ewod_dogfood.py``'s ``_grid_footprint`` rows/cols):
    ``per_side`` pads walk each of the 4 edges of the rectangle
    ``[-x_half, x_half] x [-y_half, y_half]`` at ``pitch`` spacing,
    starting bottom-left and going counterclockwise -- the real HV507-
    class part gr339236 was found on is a ring package, not a grid, and a
    grid-shaped stand-in cannot exercise the same sink pad geometry."""
    span = (per_side - 1) * pitch
    start = -span / 2.0
    positions: list[tuple[float, float]] = []
    for i in range(per_side):  # bottom edge, left -> right
        positions.append((start + i * pitch, -y_half))
    for i in range(per_side):  # right edge, bottom -> top
        positions.append((x_half, start + i * pitch))
    for i in range(per_side):  # top edge, right -> left
        positions.append((-(start + i * pitch), y_half))
    for i in range(per_side):  # left edge, top -> bottom
        positions.append((-x_half, -(start + i * pitch)))
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


def _ring_design() -> dict[str, Any]:
    return {
        "generators": [
            {
                "name": "ARR1",
                "generator": "ewod_pad_array",
                "params": {
                    "grid": [8, 8],
                    "pad_sizes": [{"name": "RESV", "cells": [[0, 0], [0, 1]]}],
                    "sink_grid": {
                        "part": _RING_LCSC,
                        "channels_per_sink": 64,  # one sink for the whole 8x8 field
                        "channel_pins": _RING_CHANNELS,
                        "serial_in_pin": "DIOA",
                        "serial_out_pin": "DIOB",
                        "top_plate_pin": "VPP",
                        "power": {"VDD": "VDD_LOGIC", "GND": "GND"},
                    },
                },
            }
        ],
        "components": [],
        "footprints": [],
        "nets": [],
        "connections": [],
        "features": [
            {
                "ftype": "outline",
                "geom": {"path": [[-20, -20], [20, -20], [20, 20], [-20, 20]]},
            },
        ],
    }


def _ring_seed(pcb) -> str:
    pcb.put(id="ewod-ring-1", args=_ring_design())
    pcb.store.part_footprint_put(
        _RING_LCSC,
        _ring_footprint(_RING_PINS, per_side=20, x_half=8.5, y_half=11.5),
    )
    return "ewod-ring-1"


@pytest.fixture
def pcb(store):
    return PcbHandler(hub=Hub(store=store))


def _route_and_count(pcb, store) -> tuple[dict[str, str], int, int]:
    slug = _ring_seed(pcb)
    ref = store.get_ref(kind="pcb", id=slug)
    assert ref is not None
    resp = pcb.put(id=slug, args={"op": "route", "seed": 1})
    assert "enqueued" in resp.body
    _drain_one_job(store, ref.id)
    status = {str(r["name"]): str(r["status"]) for r in store.pcb_route_status(ref.id)}
    escape_pins, offered = _island_terminals_offered(pcb, store, ref.id)
    return status, escape_pins, offered


def _island_terminals_offered(pcb, store, ref_id: int) -> tuple[int, int]:
    """(electrode pins with authored fixed copper on their own net, how
    many of those appear in :func:`~precis.pcb.connectivity.
    fixed_copper_pin_terminals`'s output) — the direct check that a
    terminal was OFFERED, independent of whether the router's own
    occupancy search then finds a path to it (a separate question this
    fix does not touch)."""
    design = store.pcb_load(ref_id)
    board_id = int(design["board"]["board_id"])
    layer_names = [str(layer["name"]) for layer in design["board"]["stackup"]]
    pads = pcb._drc_pads(ref_id, layer_names)
    copper = store.pcb_fixed_copper_list(board_id)
    model = {"layers": layer_names, "pads": pads, "copper": copper}
    terminals = pcb_connectivity.fixed_copper_pin_terminals(model)

    fixed_nets = {str(row["net"]) for row in copper if row.get("net")}
    # `refdes == "ARR1"` (exact), not a prefix match: the array is ONE
    # component under that exact refdes (generators.py's own "one
    # component, one pad per pin" docstring); `ARR1_SINK_0`'s channel
    # pins share the SAME net names but sit nowhere near the stub/via, so
    # a prefix match would wrongly count them as pins a terminal should
    # have been offered for.
    escape_pins = {
        (str(p["refdes"]), str(p["pin"]))
        for p in pads
        if str(p.get("refdes", "")) == "ARR1" and str(p.get("net", "")) in fixed_nets
    }
    assert escape_pins, "no electrode pin has fixed copper on its own net"
    return len(escape_pins), len(escape_pins & set(terminals.keys()))


@pytest.mark.slow
def test_ring_sink_route_op_realizes_more_escape_nets_with_polygon_touch(
    pcb, store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same board, routed twice: once with the OLD inscribed-circle-
    only touch test (`_pad_poly` forced to always-``None``, reproducing
    every pad's behaviour before this fix) and once with the real fix —
    same generator, same ring sink, same seed. The polygon-aware touch
    test must offer strictly more electrode escape nets a route, proving
    the fix changes what the ROUTER does, not merely what
    :mod:`precis.pcb.connectivity` reports in isolation. It must also
    offer an island terminal for EVERY electrode pin with fixed copper —
    the OLD circle-only test offers none at all (the exact gr339236
    symptom), the fix offers all of them (whether the router's own
    occupancy search then finds a path is independent, per (2) below)."""
    monkeypatch.setattr(pcb_connectivity, "_pad_poly", lambda pad: None)
    before, before_pins, before_offered = _route_and_count(pcb, store)
    monkeypatch.undo()
    after, after_pins, after_offered = _route_and_count(pcb, store)
    assert after_pins == before_pins
    # Every electrode pin with fixed copper on its own net gets an island
    # terminal with the fix, whether its own escape direction is straight
    # (whose stub anchors at an edge MIDPOINT the old inscribed-circle
    # test already reached, hence `before_offered` is not necessarily 0)
    # or diagonal (whose stub anchors at a CORNER only the polygon-aware
    # test reaches — gr339236's own geometry).
    assert after_offered == after_pins, (
        f"the fix still leaves {after_pins - after_offered}/{after_pins} electrode "
        "pins with fixed copper but no island terminal offered"
    )
    assert after_offered > before_offered, (
        f"the polygon-aware touch test offered no MORE island terminals than "
        f"the old circle-only test — {before_offered}/{before_pins} before, "
        f"{after_offered}/{after_pins} after"
    )

    escape_nets = [n for n in after if n.startswith("ARR1_R")]
    assert len(escape_nets) >= 50, f"too few escape nets seeded: {sorted(after)[:20]}"
    assert escape_nets == sorted(n for n in before if n.startswith("ARR1_R"))

    before_realized = {n for n in escape_nets if before[n] == "realized"}
    after_realized = {n for n in escape_nets if after[n] == "realized"}
    diag = (
        f"before: {dict(collections.Counter(before.values()))}; "
        f"after: {dict(collections.Counter(after.values()))}"
    )
    # A COUNT comparison, not a strict-superset one. The ring sink is a
    # bottom-side component (``generators.py``'s own ``sink_grid`` emits
    # it with ``layer='bottom'``), so this fixture also exercises the
    # maze router's own per-pad layer claim (gr341516's router-side
    # sibling, closed the same day this file's docstring was last
    # touched: a bottom-mounted pin's pad now claims/searches on its real
    # B.Cu cell instead of a shared F.Cu one). That shifts the WHOLE
    # board's occupancy-grid congestion, and this module's own docstring
    # is explicit that congestion outcomes are order/claim-dependent, not
    # monotonic in any one input: more nets realize overall (asserted
    # below), but the exact SET need not be a superset -- one net can
    # lose a race it used to win even as the aggregate strictly improves.
    assert len(after_realized) > len(before_realized), (
        f"the polygon-aware touch test realized no MORE escape nets than "
        f"the old circle-only test — {diag}"
    )
    print(
        f"gr339236: {len(before_realized)}/{len(escape_nets)} realized before, "
        f"{len(after_realized)}/{len(escape_nets)} after; island terminals "
        f"offered {before_offered}/{before_pins} before, "
        f"{after_offered}/{after_pins} after — {diag}"
    )

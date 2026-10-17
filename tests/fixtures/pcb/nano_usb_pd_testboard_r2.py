"""The USB-C PD Nano test board, revision 2 — synthetic place+route
fixture for ``docs/backlog/pcb-usb-c-pd-nano-testboard.md``. The original
board (``nano_usb_pd_testboard.py``) stays untouched as the router
regression fixture; this is the reviewed design.

The doc's three design corrections are folded into this netlist, not
deferred:

* **Fixed PDO set is 5/9/15/20 V** — no 12 V rail exists on the board.
* **One contract.** The STUSB4500 negotiates 20 V; VBUS *is* 20 V. The
  15/9/5 V rails are three buck stages from VBUS, not parallel contracts.
  The 5 V buck is load-bearing: it feeds the ATmega328P, which cannot run
  from vSafe5V once VBUS moves to 20 V.
* **High-side, not low-side, outputs.** Each switched channel is a
  P-FET + level-shifter pair on the 20 V rail — see below.

**Outline (doc's "as big as it needs to be" ruling): 100 x 80 mm**, not
the Nano's 18 x 45. The rationale, recorded per the doc's ask: four
switched 20 V / 5 A channels plus a 5 A VBUS trunk resolve to ~2.8 mm
external tracks under ``rules.ipc2221_track_width_mm`` (1 oz, 10 °C
rise), three buck stages carry inductor + input/output capacitor area,
and the two Nano headers pin a fixed 15.24 mm row spacing; generous
rather than tight, because the anneal item
(``pcb-anneal-closes-router-loop.md``) owns shrink/grow experiments —
this fixture supplies the honest measured baseline, not a pre-tuned
placement.

**Parts.** Real LCSC C-numbers are used where this fixture supplies a
``part_footprints`` cache row (``CATALOG_FOOTPRINTS``); the footprint
data itself is HAND-AUTHORED stand-in geometry shaped like the real
package — same discipline as ``tests/test_pcb_ewod_dogfood.py`` — not a
datasheet-accurate land pattern, and not a BOM commitment. Everything
else uses either a design-local ``footprints[]`` entry (headers, screw
terminals, inductors, the electrolytic, the crystal, SMA diodes, test
points — real drilled/land geometry) or the ``landpattern`` synthesized
bound (the small 2-pin passives and LEDs), flagged in each component's
``note`` and listed in ``meta.synthesized_footprint_refdes``.

**High-side topology (netlist review, slice 2).** Each switched channel is
a P-FET with its source on VBUS and its drain on OUTn, held off by a 10 k
gate-source resistor (R_GS) in parallel with a 12 V zener (DZn, anode on
the gate, cathode on the source). A small N-FET level shifter (gate = Nano
D4..D7) pulls the P-FET gate down through a 1 k series resistor (R_G). The
pull-up/series ratio gives ~4.5 V of |Vsg| at a 5 V VBUS (ERR_SOURCE case,
and Q_SNK at vSafe5V before the contract) and the zener clamps |Vsg| to
12 V at 20 V, inside a +/-20 V gate rating. A 100 k pulldown on the
level-shifter gate keeps every channel OFF while the MCU pin floats
(reset, ICSP). The original MIC5019 charge-pump + N-FET choice is gone: it
was a geometry stand-in whose operation on a 20 V source from a 5 V supply
was never verified. **No P-FET / N-FET MPN or LCSC number is selected**
(nothing here is a verified catalog mapping; FET intake is an open item),
so the FETs use design-local land patterns and the parts stay PROVISIONAL
in ``meta.provisional``. A second P-FET, ``Q_SNK``, is the sink power
switch with the same gate network. STUSB4500 VBUS_EN_SNK pulls its gate
down through R_SNK_G and the blocking diode D_SNK (cathode on the pin), so
the pin never sees the 20 V off-state: its absolute-maximum rating could
not be confirmed (no datasheet text reachable), so the diode is the
conservative clamp. The bucks, outputs and 20 V LED sit on ``VBUS`` only
after the contract; the connector side is ``VBUS_RAW`` (vSafe5V before the
contract). Flyback
diodes are authored (the doc: decide once loads are known — the footprint
stays even if unstuffed). Channel enables moved to D4..D7 and the error
LEDs to D8/D9, so D11..D13 carry only the ICSP bus.

**Error LEDs** follow the doc's advertised-capabilities table: the MCU
reads the STUSB4500's PDO status over I2C and drives ERR_CABLE (20 V/3 A
PDO but no 5 A → cable not e-marked) / ERR_SOURCE (no 20 V PDO at all →
source is the limit). Rail LEDs sit on the rails themselves.

Nano-compatible headers: J_N1/J_N2 carry the real 30-pin Nano mapping
(J1: TX, RX, RST, GND, D2-D12; J2: D13, 3V3, AREF, A0-A7, +5V, RST, GND,
VIN) in one rigid ``nano_hdrs`` group at the real 15.24 mm row spacing.
The 3V3 pin is declared ``unused`` — a real Nano's 3.3 V comes from its
USB-serial IC, which this board does not carry; wiring it to +5 V would
be a silent lie. ``VIN`` maps to VBUS (the negotiated 20 V rail — a real
Nano's regulator input tops out ~12 V; exposed deliberately, the board's
purpose is exposing the contract).
"""

from __future__ import annotations

from typing import Any

BOARD_W_MM = 100.0
BOARD_H_MM = 80.0

# ---------------------------------------------------------------------------
# Cached catalog footprints (part_footprints row shape: pads keyed by pad
# "number", pin_map pad number -> {name, tags}). Stand-in geometry, module
# docstring above. Pin_map "tags" land on the declare-all completed pins, so
# the nc/unused vocabulary lives here, not on the components.
# ---------------------------------------------------------------------------


def _pad(
    number: str,
    x: float,
    y: float,
    w: float,
    h: float,
    *,
    shape: str = "RECT",
    drill: float | None = None,
) -> dict[str, Any]:
    return {
        "number": number,
        "shape": shape,
        "x": x,
        "y": y,
        "w": w,
        "h": h,
        "rot": 0.0,
        "layer": "F.Cu",
        "drill": drill,
    }


def _footprint(pads: list[dict[str, Any]], pin_map: dict[str, Any]) -> dict[str, Any]:
    numbers = {p["number"] for p in pads}
    missing = set(pin_map) - numbers
    assert not missing, f"pin_map keys with no pad: {missing}"
    return {"pads": pads, "pin_map": pin_map, "source": "fixture"}


#: One pin entry in a footprint's pin list: the pin name, or
#: ``(name, tags)`` when the pin needs an intent marker (``nc``/``unused``)
#: on the declare-all completed pin.
_PinName = str | tuple[str, tuple[str, ...]]


def _pin_entry(entry: _PinName) -> dict[str, Any]:
    if isinstance(entry, str):
        return {"name": entry, "tags": []}
    name, tags = entry
    return {"name": name, "tags": list(tags)}


def _qfp(
    pin_names: list[_PinName],
    *,
    pitch: float,
    half: float,
    pad_w: float,
    pad_h: float,
) -> dict[str, Any]:
    """Four-side gull-wing footprint: pads numbered 1..N counterclockwise —
    left side top→bottom, bottom left→right, right bottom→top, top
    right→left (the IC numbering convention)."""
    n = len(pin_names)
    per_side = n // 4
    assert n % 4 == 0, "this helper only does four equal sides"
    span = (per_side - 1) * pitch
    pads: list[dict[str, Any]] = []
    pin_map: dict[str, Any] = {}
    # `pad_w` is the dimension ALONG the pin row (pitch-constrained);
    # `pad_h` is the radial extent pointing out of the body. Left/right
    # side pads swap them (long axis along x), top/bottom keep them.
    for i in range(per_side):  # left, top to bottom
        pads.append(_pad(str(i + 1), -half, span / 2 - i * pitch, pad_h, pad_w))
    for i in range(per_side):  # bottom, left to right
        b = per_side + i + 1
        pads.append(_pad(str(b), -span / 2 + i * pitch, -half, pad_w, pad_h))
    for i in range(per_side):  # right, bottom to top
        b = 2 * per_side + i + 1
        pads.append(_pad(str(b), half, -span / 2 + i * pitch, pad_h, pad_w))
    for i in range(per_side):  # top, right to left
        b = 3 * per_side + i + 1
        pads.append(_pad(str(b), span / 2 - i * pitch, half, pad_w, pad_h))
    for p in pads:
        pin_map[p["number"]] = _pin_entry(pin_names[int(p["number"]) - 1])
    return _footprint(pads, pin_map)


def _dual(
    pin_names: list[_PinName],
    *,
    pitch: float,
    half: float,
    pad_w: float,
    pad_h: float,
) -> dict[str, Any]:
    """SOIC/SOT-style dual row: left column top→bottom then right column
    bottom→top (CCW from pin 1)."""
    n = len(pin_names)
    per_row = (n + 1) // 2
    span = (per_row - 1) * pitch
    pads: list[dict[str, Any]] = []
    for i in range(per_row):
        pads.append(_pad(str(i + 1), -half, span / 2 - i * pitch, pad_w, pad_h))
    for i in range(n - per_row):
        pads.append(
            _pad(str(per_row + i + 1), half, -span / 2 + i * pitch, pad_w, pad_h)
        )
    pin_map: dict[str, Any] = {}
    for p in pads:
        pin_map[p["number"]] = _pin_entry(pin_names[int(p["number"]) - 1])
    return _footprint(pads, pin_map)


# TYPE-C-31-M-12 stand-in: 16 SMD signal pads (two 0.5 mm rows) + four
# drilled shell anchors. Pin names follow the real part's signal map
# (4x VBUS, 4x GND, CC1/CC2, DP/DN pairs, SBU pair); pad numbering is the
# fixture's own row order, NOT the datasheet's A1-A12/B1-B12 scheme. USB
# data lines are tagged `unused` — this board sinks power only.
_USBC_PADS: list[dict[str, Any]] = []
_USBC_ROW_NAMES: tuple[list[_PinName], list[_PinName]] = (
    [
        "GND",
        ("SBU1", ("unused",)),
        ("DN1", ("unused",)),
        ("DP1", ("unused",)),
        "VBUS",
        "VBUS",
        "CC1",
        "GND",
    ],
    [
        "GND",
        ("SBU2", ("unused",)),
        ("DN2", ("unused",)),
        ("DP2", ("unused",)),
        "VBUS",
        "VBUS",
        "CC2",
        "GND",
    ],
)
_USBC_PIN_MAP: dict[str, Any] = {}
for _row, _y in ((0, 0.55), (1, -0.55)):
    for _i in range(8):
        _num = str(_row * 8 + _i + 1)
        _USBC_PADS.append(_pad(_num, -1.75 + _i * 0.5, _y, 0.3, 0.8))
        _entry = _USBC_ROW_NAMES[_row][_i]
        if isinstance(_entry, str):
            _USBC_PIN_MAP[_num] = {"name": _entry, "tags": []}
        else:
            _name, _tags = _entry
            _USBC_PIN_MAP[_num] = {"name": _name, "tags": list(_tags)}
for _i, (_x, _y) in enumerate(((-3.6, 2.2), (3.6, 2.2), (-3.6, -2.2), (3.6, -2.2))):
    _num = f"S{_i + 1}"
    _USBC_PADS.append(_pad(_num, _x, _y, 1.6, 1.6, shape="ELLIPSE", drill=0.9))
    _USBC_PIN_MAP[_num] = {"name": _num, "tags": []}

# STUSB4500QTR stand-in: QFN-24 4x4, 0.5 mm pitch + exposed pad. Pin names
# are the real part's functional names for the pins this board wires;
# pad numbering is the fixture's own CCW order, NOT the datasheet's.
# Pins not wired are `nc`/`unused`-tagged so the completed netlist's
# unconnected_pin evidence names only real TODOs.
_STUSB4500_PINS: list[_PinName] = [
    "CC1",
    "CC2",
    ("NC1", ("nc",)),
    "VSYS",
    "DISCH",
    "GND",  # 1-6
    "VREG_2V7",
    "VREG_1V2",
    "GND",
    ("NC2", ("nc",)),
    "SCL",
    "SDA",  # 7-12
    "INT_N",
    ("NC3", ("nc",)),
    ("POWER_OK2", ("unused",)),
    ("POWER_OK3", ("unused",)),
    ("A_B_SIDE", ("unused",)),
    "VBUS_EN_SNK",
    ("CTL0", ("unused",)),
    ("CTL1", ("unused",)),
    ("CTL2", ("unused",)),
    ("NC4", ("nc",)),
    ("NC5", ("nc",)),
    "VDD",  # 19-24
]
_STUSB4500_RING = _qfp(_STUSB4500_PINS, pitch=0.5, half=2.2, pad_w=0.22, pad_h=0.6)
_STUSB4500 = _footprint(
    _STUSB4500_RING["pads"] + [_pad("25", 0.0, 0.0, 2.6, 2.6)],
    {**_STUSB4500_RING["pin_map"], "25": {"name": "GND", "tags": []}},
)

# ATMEGA328P-AU stand-in: TQFP-32 7x7 body, 0.8 mm pitch, real pin order.
_ATMEGA328P = _qfp(
    [
        "PD3",
        "PD4",
        "GND",
        "VCC",
        "GND",
        "VCC",
        "PB6",
        "PB7",
        "PD5",
        "PD6",
        "PD7",
        "PB0",
        "PB1",
        "PB2",
        "PB3",
        "PB4",
        "PB5",
        "AVCC",
        "ADC6",
        "AREF",
        "GND",
        "ADC7",
        "PC0",
        "PC1",
        "PC2",
        "PC3",
        "PC4",
        "PC5",
        "PC6",
        "PD0",
        "PD1",
        "PD2",
    ],
    pitch=0.8,
    half=4.35,
    pad_w=0.45,
    pad_h=1.5,
)

# TPS54360DDAR stand-in: SOIC-8-EP. Real pin order: BOOT, VIN, EN,
# RT/CLK, FB, COMP, GND, SW; pad 9 is the exposed pad (GND).
_TPS54360_PINS: list[_PinName] = [
    "BOOT",
    "VIN",
    "EN",
    ("RTCLK", ("unused",)),
    "FB",
    "COMP",
    "GND",
    "SW",
]
_TPS54360_BODY = _dual(_TPS54360_PINS, pitch=1.27, half=2.9, pad_w=2.2, pad_h=0.6)
_TPS54360 = _footprint(
    _TPS54360_BODY["pads"] + [_pad("9", 0.0, 0.0, 3.4, 2.4)],
    {**_TPS54360_BODY["pin_map"], "9": {"name": "GND", "tags": []}},
)

#: ``{lcsc: part_footprints row}`` — the test seeds these via
#: ``store.part_footprint_put`` BEFORE the put, so the design's catalog
#: parts resolve to real (stand-in) pad geometry rather than a
#: ``landpattern`` bound. Read-only lookups at apply time; no network.
CATALOG_FOOTPRINTS: dict[str, dict[str, Any]] = {
    "C165948": _footprint(_USBC_PADS, _USBC_PIN_MAP),
    "C2678061": _STUSB4500,
    "C14877": _ATMEGA328P,
    "C44377": _TPS54360,
}

# ---------------------------------------------------------------------------
# Design-local footprints (the put payload's `footprints[]` block) — real
# drilled geometry for the mechanical parts.
# ---------------------------------------------------------------------------


def _local(name: str, pads: list[dict[str, Any]], **kw: Any) -> dict[str, Any]:
    return {"name": name, "pads": pads, **kw}


def _thp(pin: str, x: float, y: float, dia: float, drill: float) -> dict[str, Any]:
    return {"pin": pin, "shape": "circle", "x": x, "y": y, "w": dia, "drill": drill}


def _smdp(pin: str, x: float, y: float, w: float, h: float) -> dict[str, Any]:
    return {"pin": pin, "shape": "rect", "x": x, "y": y, "w": w, "h": h}


FOOTPRINTS: list[dict[str, Any]] = [
    _local(
        "HDR-2.54-1x15",
        [_thp(str(i + 1), (i - 7) * 2.54, 0.0, 1.7, 0.9) for i in range(15)],
    ),
    _local(
        "ICSP-2.54-2x3",
        [
            _thp("1", -2.54, 1.27, 1.7, 0.9),  # MISO
            _thp("2", -2.54, -1.27, 1.7, 0.9),  # VCC
            _thp("3", 0.0, 1.27, 1.7, 0.9),  # SCK
            _thp("4", 0.0, -1.27, 1.7, 0.9),  # MOSI
            _thp("5", 2.54, 1.27, 1.7, 0.9),  # RST
            _thp("6", 2.54, -1.27, 1.7, 0.9),  # GND
        ],
    ),
    _local(
        "SCREW-5.08-2P",
        [_thp("1", -2.54, 0.0, 2.2, 1.3), _thp("2", 2.54, 0.0, 2.2, 1.3)],
    ),
    _local(
        "IND-7x7",
        [_smdp("1", -2.9, 0.0, 2.6, 3.2), _smdp("2", 2.9, 0.0, 2.6, 3.2)],
    ),
    _local(
        "CAP-ELEC-5P",
        [_thp("1", -2.5, 0.0, 1.8, 0.8), _thp("2", 2.5, 0.0, 1.8, 0.8)],
    ),
    _local(
        "CAP-1206",
        [_smdp("1", -1.5, 0.0, 1.6, 1.8), _smdp("2", 1.5, 0.0, 1.6, 1.8)],
    ),
    _local(
        "XTAL-3225",
        [_smdp("1", -1.05, 0.0, 1.4, 1.3), _smdp("2", 1.05, 0.0, 1.4, 1.3)],
    ),
    _local(
        "DIODE-SMA",
        [_smdp("A", -1.55, 0.0, 1.8, 1.7), _smdp("K", 1.55, 0.0, 1.8, 1.7)],
    ),
    _local("TP-THT", [_thp("1", 0.0, 0.0, 1.6, 0.9)]),
    _local(
        "DIODE-SOD123",
        [_smdp("A", -1.6, 0.0, 1.0, 1.2), _smdp("K", 1.6, 0.0, 1.0, 1.2)],
    ),
    # Part-agnostic DPAK / SOT-23 lands for the P-FETs and the level-shift
    # N-FET: no LCSC number is bound (see module docstring, slice 2).
    _local(
        "DPAK-3-GDS",
        [
            _smdp("G", -2.286, 3.4, 1.1, 2.0),
            _smdp("D", 0.0, -1.5, 5.8, 4.6),  # drain tab
            _smdp("S", 2.286, 3.4, 1.1, 2.0),
        ],
    ),
    _local(
        "SOT23-3-GSD",
        [
            _smdp("G", -0.95, 1.0, 0.8, 0.9),
            _smdp("S", 0.95, 1.0, 0.8, 0.9),
            _smdp("D", 0.0, -1.0, 0.8, 0.9),
        ],
    ),
]

# ---------------------------------------------------------------------------
# Components
# ---------------------------------------------------------------------------

_PART_TBD_NOTE = (
    "topology resolved at netlist review (slice 2); the MPN/LCSC part is NOT "
    "yet selected — design-local land pattern, no catalog part bound"
)

#: Gate-network resistors (ohms). A P-FET switching a 20 V source must not
#: see |Vgs| above 15 V (inside a typical +/-20 V rating): a zener clamps
#: the drive at GATE_CLAMP_V, and below the clamp the series/pull-up pair
#: gives VBUS * R_GS / (R_G + R_GS) (~4.5 V at a 5 V VBUS). Shared by
#: the four output channels and the sink power switch; the structural test
#: re-derives the on-state Vgs from these.
GATE_SERIES_OHMS = 1_000
GATE_SOURCE_OHMS = 10_000
GATE_PULLDOWN_OHMS = 100_000
GATE_CLAMP_V = 12.0
MAX_ABS_VGS_V = 15.0
_SYNTH_NOTE = "synthesized landpattern bound (no catalog part / no authored footprint)"

_RAILS = ("15", "9", "5")  # bucks from VBUS; 20 V is VBUS itself
_BUCK_CURRENT_A = {"15": 2.0, "9": 2.0, "5": 3.0}
_BUCK_LABEL = "TPS54360 60V/3.5A buck"


def _two_pin() -> list[dict[str, Any]]:
    return [{"name": "1"}, {"name": "2"}]


def _led_pins() -> list[dict[str, Any]]:
    return [{"name": "A"}, {"name": "K"}]


def _buck_comps(suffix: str) -> list[dict[str, Any]]:
    """One buck stage's passives — input/output/boot/comp caps and the
    feedback divider + enable pull-up, all shared across the three rails."""
    return [
        {
            "refdes": f"L{suffix}",
            "label": "IND-SMD-10uH",
            "footprint": "IND-7x7",
            "pins": [],
        },
        {
            "refdes": f"C_IN{suffix}",
            "label": "CAP-1206-10uF-35V",
            "footprint": "CAP-1206",
            "pins": [],
        },
        {
            "refdes": f"C_O{suffix}",
            "label": "CAP-1206-22uF-25V",
            "footprint": "CAP-1206",
            "pins": [],
        },
        {
            "refdes": f"C_B{suffix}",
            "label": "CAP-0402-100nF",
            "pins": _two_pin(),
            "note": _SYNTH_NOTE,
        },
        {
            "refdes": f"C_COMP{suffix}",
            "label": "CAP-0402-10nF",
            "pins": _two_pin(),
            "note": _SYNTH_NOTE,
        },
        {
            "refdes": f"R_FBT{suffix}",
            "label": "RES-0402-FBT",
            "pins": _two_pin(),
            "note": _SYNTH_NOTE,
        },
        {
            "refdes": f"R_FBB{suffix}",
            "label": "RES-0402-FBB",
            "pins": _two_pin(),
            "note": _SYNTH_NOTE,
        },
        {
            "refdes": f"R_EN{suffix}",
            "label": "RES-0402-100k",
            "pins": _two_pin(),
            "note": _SYNTH_NOTE,
        },
    ]


def _hs_channel_comps(i: int) -> list[dict[str, Any]]:
    """One switched high-side channel (netlist review, slice 2): a P-FET
    between VBUS and the output, pulled off through R_GS; a small N-FET
    level shifter (MCU pin -> QL gate) pulls the P-FET gate down through
    R_G to switch it on. R_PD holds the N-FET gate low so the channel is
    OFF while the MCU pin floats (reset / ISP). Flyback diode: populate iff
    the load is inductive (open question, footprint stays)."""
    return [
        {
            "refdes": f"Q{i}",
            "label": "P-FET 40V high-side switch DPAK",
            "footprint": "DPAK-3-GDS",
            "pins": [],
            "note": _PART_TBD_NOTE,
        },
        {
            "refdes": f"QL{i}",
            "label": "N-FET level shifter SOT-23",
            "footprint": "SOT23-3-GSD",
            "pins": [],
            "note": _PART_TBD_NOTE,
        },
        {
            "refdes": f"R_G{i}",
            "label": "RES-0603-1k gate series",
            "pins": _two_pin(),
            "note": _SYNTH_NOTE,
        },
        {
            "refdes": f"R_GS{i}",
            "label": "RES-0603-10k gate-source (holds P-FET off)",
            "pins": _two_pin(),
            "note": _SYNTH_NOTE,
        },
        {
            "refdes": f"DZ{i}",
            "label": "ZENER-SOD123-12V gate clamp",
            "footprint": "DIODE-SOD123",
            "pins": [],
            "note": f"caps |Vsg| of Q{i} at 12 V on a 20 V rail",
        },
        {
            "refdes": f"R_PD{i}",
            "label": "RES-0603-100k level-shifter pulldown (off in reset)",
            "pins": _two_pin(),
            "note": _SYNTH_NOTE,
        },
        {
            "refdes": f"J_O{i}",
            "label": "SCREW-TERM-5.08-2P",
            "footprint": "SCREW-5.08-2P",
            "pins": [],
        },
        {
            "refdes": f"D_F{i}",
            "label": "DIODE-SMA-40V flyback",
            "footprint": "DIODE-SMA",
            "pins": [],
            "note": "flyback: populate only if the load is inductive "
            "(doc §Open questions); footprint authored regardless",
        },
    ]


def _sink_switch_comps() -> list[dict[str, Any]]:
    """STUSB4500 VBUS_EN_SNK sink power path: P-FET isolating the loads
    from the pre-contract vSafe5V stage, same gate network as the channels."""
    return [
        {
            "refdes": "Q_SNK",
            "label": "P-FET 40V sink power switch DPAK",
            "footprint": "DPAK-3-GDS",
            "pins": [],
            "note": _PART_TBD_NOTE,
        },
        {
            "refdes": "R_SNK_G",
            "label": "RES-0603-1k gate series",
            "pins": _two_pin(),
            "note": _SYNTH_NOTE,
        },
        {
            "refdes": "DZ_SNK",
            "label": "ZENER-SOD123-12V gate clamp",
            "footprint": "DIODE-SOD123",
            "pins": [],
            "note": "caps |Vsg| of Q_SNK at 12 V",
        },
        {
            "refdes": "D_SNK",
            "label": "DIODE-SOD123 blocking (pin never sees VBUS)",
            "footprint": "DIODE-SOD123",
            "pins": [],
            "note": "VBUS_EN_SNK abs-max not confirmed: blocks the 20 V "
            "off-state from the open-drain pin",
        },
        {
            "refdes": "R_SNK_GS",
            "label": "RES-0603-10k gate-source (holds switch off)",
            "pins": _two_pin(),
            "note": _SYNTH_NOTE,
        },
    ]


def _components() -> list[dict[str, Any]]:
    comps: list[dict[str, Any]] = [
        # --- USB-C + PD sink ----------------------------------------------
        {
            "refdes": "J_USB",
            "label": "USB-C receptacle TYPE-C-31-M-12",
            "part": "C165948",
            "pins": [],
            "edge_mate": {"face": "S", "require": "required"},
        },
        {
            "refdes": "U_PD",
            "label": "STUSB4500QTR PD sink controller",
            "part": "C2678061",
            "pins": [],
        },
        {
            "refdes": "C_PD",
            "label": "CAP-1206-10uF-35V",
            "footprint": "CAP-1206",
            "pins": [],
        },
        {
            "refdes": "C_BULK",
            "label": "CAP-ELEC-100uF-35V-THT",
            "footprint": "CAP-ELEC-5P",
            "pins": [],
        },
        {
            "refdes": "C_VREG12",
            "label": "CAP-0402-1uF",
            "pins": _two_pin(),
            "note": _SYNTH_NOTE,
        },
        {
            "refdes": "C_VREG27",
            "label": "CAP-0402-1uF",
            "pins": _two_pin(),
            "note": _SYNTH_NOTE,
        },
        # --- bucks: 15/9/5 from VBUS (the one contract is 20 V) ------------
        *(
            c
            for suffix in _RAILS
            for c in (
                [
                    {
                        "refdes": f"U_B{suffix}",
                        "label": _BUCK_LABEL,
                        "part": "C44377",
                        "pins": [],
                        "note": f"VBUS -> V{suffix} buck",
                    }
                ]
                + _buck_comps(suffix)
            )
        ),
        # --- MCU (5 V rail is load-bearing) --------------------------------
        {"refdes": "U1", "label": "ATMEGA328P-AU", "part": "C14877", "pins": []},
        {
            "refdes": "Y1",
            "label": "XTAL-3225-16MHz",
            "footprint": "XTAL-3225",
            "pins": [],
        },
        {
            "refdes": "C_X1",
            "label": "CAP-0402-22pF",
            "pins": _two_pin(),
            "note": _SYNTH_NOTE,
        },
        {
            "refdes": "C_X2",
            "label": "CAP-0402-22pF",
            "pins": _two_pin(),
            "note": _SYNTH_NOTE,
        },
        {
            "refdes": "C_VCC",
            "label": "CAP-0402-100nF",
            "pins": _two_pin(),
            "note": _SYNTH_NOTE,
        },
        {
            "refdes": "C_AVCC",
            "label": "CAP-0402-100nF",
            "pins": _two_pin(),
            "note": _SYNTH_NOTE,
        },
        {
            "refdes": "C_AREF",
            "label": "CAP-0402-100nF",
            "pins": _two_pin(),
            "note": _SYNTH_NOTE,
        },
        {
            "refdes": "C_RST",
            "label": "CAP-0402-100nF",
            "pins": _two_pin(),
            "note": _SYNTH_NOTE,
        },
        {
            "refdes": "R_RST",
            "label": "RES-0402-10k",
            "pins": _two_pin(),
            "note": _SYNTH_NOTE,
        },
        {
            "refdes": "R_PU_SCL",
            "label": "RES-0402-4k7",
            "pins": _two_pin(),
            "note": _SYNTH_NOTE,
        },
        {
            "refdes": "R_PU_SDA",
            "label": "RES-0402-4k7",
            "pins": _two_pin(),
            "note": _SYNTH_NOTE,
        },
        # --- Nano headers (rigid pair at the real 15.24 mm row spacing) ----
        {
            "refdes": "J_N1",
            "label": "HDR-2.54-1x15 Nano J1",
            "footprint": "HDR-2.54-1x15",
            "pins": [],
            "group": "nano_hdrs",
            "group_offset": {"x": 0.0, "y": 0.0, "rot": 90},
        },
        {
            "refdes": "J_N2",
            "label": "HDR-2.54-1x15 Nano J2",
            "footprint": "HDR-2.54-1x15",
            "pins": [{"name": "2", "unused": True}],
            "group": "nano_hdrs",
            "group_offset": {"x": 15.24, "y": 0.0, "rot": 90},
            "note": "pin 2 is Nano 3V3 — no 3.3 V rail exists; left unconnected "
            "rather than tying it to 5 V",
        },
        {
            "refdes": "J_ISP",
            "label": "ICSP-HDR-2.54-2x3",
            "footprint": "ICSP-2.54-2x3",
            "pins": [],
        },
        # --- rail exposure terminals ----------------------------------------
        *(
            {
                "refdes": f"J_R{r}",
                "label": "SCREW-TERM-5.08-2P",
                "footprint": "SCREW-5.08-2P",
                "pins": [],
                "note": f"exposes V{r}",
            }
            for r in _RAILS
        ),
        # --- rail LEDs ------------------------------------------------------
        *(
            c
            for r in ("20", *_RAILS)
            for c in (
                {
                    "refdes": f"R_L{r}",
                    "label": "RES-0603-1k",
                    "pins": _two_pin(),
                    "note": _SYNTH_NOTE,
                },
                {
                    "refdes": f"LED_V{r}",
                    "label": "LED-0603",
                    "pins": _led_pins(),
                    "note": _SYNTH_NOTE,
                },
            )
        ),
        # --- error LEDs (MCU-driven off the PDO-inspection table) -----------
        {
            "refdes": "R_EC",
            "label": "RES-0603-1k",
            "pins": _two_pin(),
            "note": _SYNTH_NOTE,
        },
        {
            "refdes": "LED_EC",
            "label": "LED-0603",
            "pins": _led_pins(),
            "note": _SYNTH_NOTE,
        },
        {
            "refdes": "R_ES",
            "label": "RES-0603-1k",
            "pins": _two_pin(),
            "note": _SYNTH_NOTE,
        },
        {
            "refdes": "LED_ES",
            "label": "LED-0603",
            "pins": _led_pins(),
            "note": _SYNTH_NOTE,
        },
        # --- rail test points ------------------------------------------------
        *(
            {
                "refdes": f"TP{r}",
                "label": "TEST-POINT-THT",
                "footprint": "TP-THT",
                "pins": [],
            }
            for r in ("20", *_RAILS)
        ),
    ]
    comps += _sink_switch_comps()
    for i in range(1, 5):
        comps += _hs_channel_comps(i)
    return comps


# ---------------------------------------------------------------------------
# Nets — per-net rules: rail currents (est_current_a -> IPC-2221 width) and
# working_voltage_v (-> pairwise clearance in check_clearance). The GATE
# nets carry the netlist-review TODO out loud.
# ---------------------------------------------------------------------------

_HS_TODO = (
    "TODO(netlist-review): topology resolved (P-FET + N-FET level shifter, "
    "slice 2) but the P-FET / N-FET parts are not selected or verified "
    "(docs/backlog/pcb-usb-c-pd-nano-testboard.md §Topology)"
)


def _nets() -> list[dict[str, Any]]:
    nets: list[dict[str, Any]] = [
        {
            "name": "VBUS",
            "class": "power",
            "current": 5.0,
            "working_voltage_v": 20.0,
            "note": "the single negotiated contract — 20 V fixed PDO, behind "
            "the Q_SNK sink power switch (VBUS_RAW is the connector side); "
            "the bucks, output switches and 20 V rail LED stay off until "
            "STUSB4500 enables VBUS_EN_SNK",
        },
        {
            "name": "VBUS_RAW",
            "class": "power",
            "current": 5.0,
            "working_voltage_v": 20.0,
            "note": "connector side of the sink switch: vSafe5V before the "
            "contract, 20 V after; feeds only the STUSB4500 and Q_SNK",
        },
        {
            "name": "SNK_GATE",
            "working_voltage_v": 20.0,
            "note": "Q_SNK gate; STUSB4500 VBUS_EN_SNK pulls it low via "
            "R_SNK_G, R_SNK_GS holds it off",
        },
        {
            "name": "SNK_GATE_MID",
            "working_voltage_v": 20.0,
            "note": "between R_SNK_G and the blocking diode D_SNK",
        },
        {
            "name": "SNK_GATE_DRV",
            "working_voltage_v": 20.0,
            "note": "STUSB4500 VBUS_EN_SNK pin side of D_SNK: only ever pulled low",
        },
        {"name": "GND", "class": "ground", "current": 5.0, "working_voltage_v": 0.0},
        {
            "name": "V15",
            "class": "power",
            "current": 2.0,
            "working_voltage_v": 15.0,
            "note": "buck from VBUS",
        },
        {
            "name": "V9",
            "class": "power",
            "current": 2.0,
            "working_voltage_v": 9.0,
            "note": "buck from VBUS",
        },
        {
            "name": "V5",
            "class": "power",
            "current": 3.0,
            "working_voltage_v": 5.0,
            "note": "buck from VBUS; powers the ATmega328P — load-bearing",
        },
    ]
    for r in _RAILS:
        nets += [
            {
                "name": f"SW{r}",
                "class": "power",
                "current": _BUCK_CURRENT_A[r],
                "working_voltage_v": 20.0,
                "note": "buck switch node; swings 0..VBUS",
            },
            {
                "name": f"BOOT{r}",
                "working_voltage_v": 25.0,
                "note": "bootstrap — floats ~5 V over SW",
            },
            {"name": f"FB{r}", "working_voltage_v": 0.8},
            {
                "name": f"EN{r}",
                "working_voltage_v": 20.0,
                "note": "enable pulled to VBUS",
            },
            {"name": f"COMP{r}", "working_voltage_v": 5.0},
        ]
    for i in range(1, 5):
        nets += [
            {
                "name": f"GATE{i}",
                "working_voltage_v": 20.0,
                "note": _HS_TODO + "; P-FET gate, at VBUS off / VBUS-12 V on",
            },
            {
                "name": f"LS{i}",
                "working_voltage_v": 20.0,
                "note": f"level-shifter N-FET drain, behind R_G{i}",
            },
            {
                "name": f"OUT{i}",
                "class": "power",
                "current": 5.0,
                "working_voltage_v": 20.0,
                "note": _HS_TODO + "; 20 V / 5 A envelope",
            },
        ]
    for name, volts, note in (
        ("CC1", 5.0, "USB-C CC line to STUSB4500"),
        ("CC2", 5.0, "USB-C CC line to STUSB4500"),
        ("SDA", 5.0, "I2C, shared with Nano A4"),
        ("SCL", 5.0, "I2C, shared with Nano A5"),
        ("RST", 5.0, None),
        ("AREF", 5.0, None),
        ("TX", 5.0, None),
        ("RX", 5.0, None),
        ("X1", 5.0, None),
        ("X2", 5.0, None),
        (
            "ERR_CABLE",
            5.0,
            "PDO table: 20 V/3 A advertised but no 5 A → cable not e-marked",
        ),
        ("ERR_SOURCE", 5.0, "PDO table: no 20 V at all → source is the limit"),
    ):
        nets.append(
            {
                "name": name,
                "working_voltage_v": volts,
                **({"note": note} if note else {}),
            }
        )
    # Nano digital pins. D11-D13 are the ICSP bus and nothing else (netlist
    # review, slice 2: channel enables moved to D4-D7, error LEDs to D8/D9).
    _d_notes = {
        4: "channel 1 switch enable (level shifter QL1 gate)",
        5: "channel 2 switch enable (level shifter QL2 gate)",
        6: "channel 3 switch enable (level shifter QL3 gate)",
        7: "channel 4 switch enable (level shifter QL4 gate)",
        8: "ERR_CABLE LED drive",
        9: "ERR_SOURCE LED drive",
        11: "ICSP MOSI only",
        12: "ICSP MISO only",
        13: "ICSP SCK only",
    }
    nets += [
        {
            "name": f"D{i}",
            "working_voltage_v": 5.0,
            **({"note": _d_notes[i]} if i in _d_notes else {}),
        }
        for i in range(2, 14)
    ]
    nets += [
        {"name": n, "working_voltage_v": 5.0}
        for n in ("A0", "A1", "A2", "A3", "A6", "A7")
    ]
    for r in ("20", *_RAILS):
        volts = 20.0 if r == "20" else float(r)
        nets.append(
            {
                "name": f"LED_V{r}",
                "working_voltage_v": volts,
                "note": "rail-present LED drop",
            }
        )
    return nets


# ---------------------------------------------------------------------------
# Connections
# ---------------------------------------------------------------------------


def _c(net: str, refdes: str, pin: str, note: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"net": net, "refdes": refdes, "pin": pin}
    if note:
        out["note"] = note
    return out


def _buck_conns(suffix: str) -> list[dict[str, Any]]:
    r = f"V{suffix}"
    sw, boot, fb, en, comp = (
        f"SW{suffix}",
        f"BOOT{suffix}",
        f"FB{suffix}",
        f"EN{suffix}",
        f"COMP{suffix}",
    )
    return [
        _c("VBUS", f"U_B{suffix}", "VIN"),
        _c("VBUS", f"C_IN{suffix}", "1"),
        _c("VBUS", f"R_EN{suffix}", "1"),
        _c(en, f"U_B{suffix}", "EN"),
        _c(en, f"R_EN{suffix}", "2"),
        _c("GND", f"U_B{suffix}", "GND"),
        _c("GND", f"C_IN{suffix}", "2"),
        _c("GND", f"C_O{suffix}", "2"),
        _c("GND", f"C_COMP{suffix}", "2"),
        _c("GND", f"R_FBB{suffix}", "2"),
        _c(boot, f"U_B{suffix}", "BOOT"),
        _c(boot, f"C_B{suffix}", "1"),
        _c(sw, f"U_B{suffix}", "SW"),
        _c(sw, f"C_B{suffix}", "2"),
        _c(sw, f"L{suffix}", "1"),
        _c(fb, f"U_B{suffix}", "FB"),
        _c(fb, f"R_FBT{suffix}", "2"),
        _c(fb, f"R_FBB{suffix}", "1"),
        _c(comp, f"U_B{suffix}", "COMP"),
        _c(comp, f"C_COMP{suffix}", "1"),
        _c(r, f"L{suffix}", "2"),
        _c(r, f"C_O{suffix}", "1"),
        _c(r, f"R_FBT{suffix}", "1"),
        _c(r, f"R_L{suffix}", "1"),
        _c(r, f"J_R{suffix}", "1"),
        _c(r, f"TP{suffix}", "1"),
        _c("GND", f"J_R{suffix}", "2"),
        _c(f"LED_V{suffix}", f"R_L{suffix}", "2"),
        _c(f"LED_V{suffix}", f"LED_V{suffix}", "A"),
        _c("GND", f"LED_V{suffix}", "K"),
    ]


def _connections() -> list[dict[str, Any]]:
    conns: list[dict[str, Any]] = [
        # --- VBUS trunk -----------------------------------------------------
        _c("VBUS_RAW", "J_USB", "VBUS"),
        _c("VBUS_RAW", "C_PD", "1"),
        _c("VBUS_RAW", "U_PD", "VDD"),
        _c("VBUS_RAW", "U_PD", "DISCH"),
        # sink power switch: P-FET, source on the connector side
        _c("VBUS_RAW", "Q_SNK", "S"),
        _c("VBUS", "Q_SNK", "D"),
        _c("SNK_GATE", "Q_SNK", "G"),
        _c("SNK_GATE", "R_SNK_GS", "1"),
        _c("VBUS_RAW", "R_SNK_GS", "2"),
        _c("SNK_GATE", "R_SNK_G", "1"),
        _c("SNK_GATE_MID", "R_SNK_G", "2"),
        _c("SNK_GATE_MID", "D_SNK", "A"),
        _c("SNK_GATE_DRV", "D_SNK", "K"),
        _c("SNK_GATE", "DZ_SNK", "A"),
        _c("VBUS_RAW", "DZ_SNK", "K"),
        _c("SNK_GATE_DRV", "U_PD", "VBUS_EN_SNK"),
        _c("VBUS", "C_BULK", "1"),
        _c("VBUS", "J_N2", "15", "Nano VIN pin — negotiated rail"),
        # --- GND ------------------------------------------------------------
        _c("GND", "J_USB", "GND"),
        _c("GND", "J_USB", "S1"),
        _c("GND", "J_USB", "S2"),
        _c("GND", "J_USB", "S3"),
        _c("GND", "J_USB", "S4"),
        _c("GND", "C_BULK", "2"),
        _c("GND", "C_PD", "2"),
        _c("GND", "U_PD", "GND"),
        _c("GND", "C_VREG12", "2"),
        _c("GND", "C_VREG27", "2"),
        _c("GND", "U1", "GND"),
        _c("GND", "C_X1", "2"),
        _c("GND", "C_X2", "2"),
        _c("GND", "C_VCC", "2"),
        _c("GND", "C_AVCC", "2"),
        _c("GND", "C_AREF", "2"),
        _c("GND", "C_RST", "2"),
        _c("GND", "J_N1", "4"),
        _c("GND", "J_N2", "14"),
        _c("GND", "J_ISP", "6"),
        # --- PD control ------------------------------------------------------
        _c("CC1", "J_USB", "CC1"),
        _c("CC1", "U_PD", "CC1"),
        _c("CC2", "J_USB", "CC2"),
        _c("CC2", "U_PD", "CC2"),
        _c("V5", "U_PD", "VSYS"),
        _c("PD_VREG_1V2", "U_PD", "VREG_1V2"),
        _c("PD_VREG_1V2", "C_VREG12", "1"),
        _c("PD_VREG_2V7", "U_PD", "VREG_2V7"),
        _c("PD_VREG_2V7", "C_VREG27", "1"),
        _c("SDA", "U_PD", "SDA"),
        _c("SCL", "U_PD", "SCL"),
        _c("D3", "U_PD", "INT_N"),
        # --- MCU power + housekeeping ----------------------------------------
        _c("V5", "U1", "VCC"),
        _c("V5", "U1", "AVCC"),
        _c("V5", "C_VCC", "1"),
        _c("V5", "C_AVCC", "1"),
        _c("AREF", "U1", "AREF"),
        _c("AREF", "C_AREF", "1"),
        _c("RST", "U1", "PC6"),
        _c("RST", "R_RST", "2"),
        _c("RST", "C_RST", "1"),
        _c("V5", "R_RST", "1"),
        _c("X1", "U1", "PB6"),
        _c("X1", "Y1", "1"),
        _c("X1", "C_X1", "1"),
        _c("X2", "U1", "PB7"),
        _c("X2", "Y1", "2"),
        _c("X2", "C_X2", "1"),
        # --- I2C -------------------------------------------------------------
        _c("SDA", "U1", "PC4"),
        _c("SDA", "J_N2", "8"),
        _c("SDA", "R_PU_SDA", "2"),
        _c("V5", "R_PU_SDA", "1"),
        _c("SCL", "U1", "PC5"),
        _c("SCL", "J_N2", "9"),
        _c("SCL", "R_PU_SCL", "2"),
        _c("V5", "R_PU_SCL", "1"),
        # --- Nano J1: TX/RX/RST/GND/D2-D12 -----------------------------------
        _c("TX", "U1", "PD1"),
        _c("TX", "J_N1", "1"),
        _c("RX", "U1", "PD0"),
        _c("RX", "J_N1", "2"),
        _c("RST", "J_N1", "3"),
        _c("D2", "U1", "PD2"),
        _c("D2", "J_N1", "5"),
        _c("D3", "U1", "PD3"),
        _c("D3", "J_N1", "6"),
        _c("D4", "U1", "PD4"),
        _c("D4", "J_N1", "7"),
        _c("D5", "U1", "PD5"),
        _c("D5", "J_N1", "8"),
        _c("D6", "U1", "PD6"),
        _c("D6", "J_N1", "9"),
        _c("D7", "U1", "PD7"),
        _c("D7", "J_N1", "10"),
        _c("D8", "U1", "PB0"),
        _c("D8", "J_N1", "11"),
        _c("D9", "U1", "PB1"),
        _c("D9", "J_N1", "12"),
        _c("D10", "U1", "PB2"),
        _c("D10", "J_N1", "13"),
        _c("D11", "U1", "PB3"),
        _c("D11", "J_N1", "14"),
        _c("D11", "J_ISP", "4"),  # MOSI
        _c("D12", "U1", "PB4"),
        _c("D12", "J_N1", "15"),
        _c("D12", "J_ISP", "1"),  # MISO
        # --- Nano J2: D13/AREF/A0-A7/+5V/RST/GND/VIN -------------------------
        _c("D13", "U1", "PB5"),
        _c("D13", "J_N2", "1"),
        _c("D13", "J_ISP", "3"),  # SCK
        _c("AREF", "J_N2", "3"),
        _c("A0", "U1", "PC0"),
        _c("A0", "J_N2", "4"),
        _c("A1", "U1", "PC1"),
        _c("A1", "J_N2", "5"),
        _c("A2", "U1", "PC2"),
        _c("A2", "J_N2", "6"),
        _c("A3", "U1", "PC3"),
        _c("A3", "J_N2", "7"),
        _c("A6", "U1", "ADC6"),
        _c("A6", "J_N2", "10"),
        _c("A7", "U1", "ADC7"),
        _c("A7", "J_N2", "11"),
        _c("V5", "J_N2", "12"),  # +5V pin
        _c("RST", "J_N2", "13"),
        _c("V5", "J_ISP", "2"),  # ICSP VCC
        _c("RST", "J_ISP", "5"),
        # --- rail LEDs / error LEDs ------------------------------------------
        _c("VBUS", "R_L20", "1"),
        _c("LED_V20", "R_L20", "2"),
        _c("LED_V20", "LED_V20", "A"),
        _c("GND", "LED_V20", "K"),
        _c("VBUS", "TP20", "1"),
        _c("D8", "R_EC", "1"),
        _c("ERR_CABLE", "R_EC", "2"),
        _c("ERR_CABLE", "LED_EC", "A"),
        _c("GND", "LED_EC", "K"),
        _c("D9", "R_ES", "1"),
        _c("ERR_SOURCE", "R_ES", "2"),
        _c("ERR_SOURCE", "LED_ES", "A"),
        _c("GND", "LED_ES", "K"),
    ]
    for suffix in _RAILS:
        conns += _buck_conns(suffix)
    for i in range(1, 5):
        en_net = f"D{3 + i}"  # channel enables on D4..D7
        conns += [
            _c(en_net, f"QL{i}", "G"),
            _c(en_net, f"R_PD{i}", "1"),
            _c("GND", f"R_PD{i}", "2"),
            _c("GND", f"QL{i}", "S"),
            _c(f"LS{i}", f"QL{i}", "D"),
            _c(f"LS{i}", f"R_G{i}", "2"),
            _c(f"GATE{i}", f"R_G{i}", "1"),
            _c(f"GATE{i}", f"Q{i}", "G"),
            _c(f"GATE{i}", f"R_GS{i}", "1"),
            _c("VBUS", f"R_GS{i}", "2"),
            _c(f"GATE{i}", f"DZ{i}", "A"),
            _c("VBUS", f"DZ{i}", "K"),
            _c("VBUS", f"Q{i}", "S"),
            _c(f"OUT{i}", f"Q{i}", "D"),
            _c(f"OUT{i}", f"J_O{i}", "1"),
            _c(f"OUT{i}", f"D_F{i}", "K"),
            _c("GND", f"J_O{i}", "2"),
            _c("GND", f"D_F{i}", "A"),
        ]
    return conns


FEATURES: list[dict[str, Any]] = [
    {
        "ftype": "outline",
        "geom": {
            "path": [
                [0, 0],
                [BOARD_W_MM, 0],
                [BOARD_W_MM, BOARD_H_MM],
                [0, BOARD_H_MM],
                [0, 0],
            ],
            "corner_radius_mm": 3.0,
        },
        "note": f"{BOARD_W_MM}x{BOARD_H_MM} mm — see module docstring for "
        "the sizing rationale (doc's outline ruling)",
    },
    *(
        {
            "ftype": "mounting_hole",
            "x": x,
            "y": y,
            "geom": {"diameter": 3.2, "ring_dia_mm": 6.0, "plated": True},
        }
        for x, y in ((6.0, 6.0), (94.0, 6.0), (6.0, 74.0), (94.0, 74.0))
    ),
]


def _meta() -> dict[str, Any]:
    return {
        "spec": "docs/backlog/pcb-usb-c-pd-nano-testboard.md",
        "outline_mm": [BOARD_W_MM, BOARD_H_MM],
        "pd_contract": "STUSB4500 negotiates 20 V; V15/V9/V5 are bucks "
        "from VBUS (one contract, derived rails; fixed PDO set 5/9/15/20)",
        "provisional": {
            "high_side_topology": "P-FET (source on VBUS) + N-FET level "
            "shifter, 1 k series / 10 k pull-up + 12 V zener gate clamp, 100 k pulldown; sink switch "
            "Q_SNK driven by STUSB4500 VBUS_EN_SNK through a blocking diode",
            "status": "PROVISIONAL parts — the topology was resolved at "
            "netlist review (slice 2) but no P-FET / N-FET MPN or LCSC "
            "number is selected or verified; design-local land patterns",
        },
        "synthesized_footprint_refdes": [
            "C_X1",
            "C_X2",
            "C_VCC",
            "C_AVCC",
            "C_AREF",
            "C_RST",
            "C_VREG12",
            "C_VREG27",
            "C_B15",
            "C_B9",
            "C_B5",
            "C_COMP15",
            "C_COMP9",
            "C_COMP5",
            "R_FBT15",
            "R_FBB15",
            "R_EN15",
            "R_FBT9",
            "R_FBB9",
            "R_EN9",
            "R_FBT5",
            "R_FBB5",
            "R_EN5",
            "R_RST",
            "R_PU_SCL",
            "R_PU_SDA",
            "R_L20",
            "R_L15",
            "R_L9",
            "R_L5",
            "R_EC",
            "R_ES",
            "R_GS1",
            "R_GS2",
            "R_GS3",
            "R_GS4",
            "R_G1",
            "R_G2",
            "R_G3",
            "R_G4",
            "R_PD1",
            "R_PD2",
            "R_PD3",
            "R_PD4",
            "R_SNK_G",
            "R_SNK_GS",
            "LED_V20",
            "LED_V15",
            "LED_V9",
            "LED_V5",
            "LED_EC",
            "LED_ES",
        ],
        "catalog_footprints": {
            lcsc: "stand-in authored geometry — see module docstring"
            for lcsc in CATALOG_FOOTPRINTS
        },
    }


def build_design() -> dict[str, Any]:
    """The complete ``put`` payload for the board (no op — batch author)."""
    return {
        "components": _components(),
        "nets": _nets()
        + [
            {"name": "PD_VREG_1V2", "working_voltage_v": 1.2},
            {"name": "PD_VREG_2V7", "working_voltage_v": 2.7},
        ],
        "connections": _connections(),
        "features": FEATURES,
        "footprints": FOOTPRINTS,
        "meta": _meta(),
    }


DESIGN = build_design()

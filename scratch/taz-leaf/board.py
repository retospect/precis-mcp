"""TAZ leaf node (td485252) -- board script, rev C (dogfood: td519926).

Lives in scratch/ (gitignored); the board goes to the precis DB, never the
repo (Reto 2026-10-26). Run:  uv run python scratch/taz-leaf/board.py
-> scratch/taz-leaf/out/{manifest.json,check.txt,batch.json}
Rev B (taz-leaf-b) is kept as board_b.py.

Rev C (Reto 2026-10-26, review of taz-leaf-b):
  - halves swapped: the combs sit next to the hanger, the electronics hang
    at the tip (heavy end down; antenna farthest from the stalk)
  - leaf 110 x 55 mm (the tip half is narrower; the supercap needs ~25 mm)
  - hanger slit moved to the leaf side of the ring: radial, exits the ring
    at (1, 5.45) in the leaf frame
  - two 5 mm zip-tie holes on the midrib (leaf frame x=11 and x=103): the
    fallback mount if the hook tears or does not fit
  - all board coordinates positive: leaf frame + (OX, OY)
  - the R*/C* passives are placed by hand then unlocked in xy
    (fixed='rot') so the topo anneal can trade them against the route
    proxies (flow preset: via estimate, backtrack, direction); the ICs,
    inductors, connectors and structures stay fixed='both' — the topo
    placer flips parts, ignores groups/near and lets free parts overlap
    fixed ones (gr520370, gr520546, gr520373)

Leaf frame: base at x=0, tip at x=LEAF_L, midrib y=0. Board = leaf + (OX, OY).

Parts (decisions in td485252 comments 2-4, fi520367):
  U1 nRF52833-QDAA  QFN40, supply config 6 (VDD, no DC/DC, no USB),
                    PS pa520104 Table 69 / Table 94 (pc5353477, pc5353540)
  U2 BQ25504        boost charger + MPPT, single inductor
                    da519848 sec. 9.2.1 typical solar application
  U3 SHT40-AD1B     RH/T, I2C 0x44
  PV1 KXOB25-05X3F  solar, consigned (JLC stock 0)
  BT1 CAP-XX DMT334R2S474M3DTA0 470 mF 4.2 V, consigned; PROVISIONAL (fi520367)
  SNS1/SNS2         wetness combs (comb.py), 4/4 mil until flex rules land
"""

from __future__ import annotations

import json
import math
import re
import sys
from pathlib import Path

from shapely import affinity
from shapely.geometry import Point, Polygon, box

import precis_geom.pcb as pcb

sys.path.insert(0, str(Path(__file__).parent))
from comb import CombSpec, comb_footprint, leaf_outline  # noqa: E402

HERE = Path(__file__).parent
SLUG = "taz-leaf-d"  # rev C geometry + sheet groups, first routed run

LEAF_L, LEAF_W = 110.0, 55.0
OX, OY = 30.0, 30.0  # leaf frame -> board frame (all positive)
SENSE_X = (4.0, 48.0)  # combs between these (leaf frame); copper-free root
ELEC_FROM_X = 50.0
RING_C, RING_R, HOLE_R, SLIT_W = (-13.0, 0.0), 15.0, 10.0, 0.8
SLIT_EXIT = (1.0, 5.45)  # Reto: slit leaves the ring here (leaf frame)
ZIP_HOLES = ((11.0, 0.0), (103.0, 0.0))  # 5 mm, zip-tie fallback mount
ZIP_D, ZIP_KEEP = 5.0, 3.0  # drill, copper-free radius
ANT_KEEP_X = 93.4  # no inner/bottom copper from here to the tip

# nRF52833 QFN40 pins by function (pa520104 Table 63, pc5353448..51; the
# catalog footprint pin_map C2895249 uses the same names).
AIN = [
    "P0.02_AIN0", "P0.03_AIN1", "P0.04_AIN2", "P0.05_AIN3",
    "P0.28_AIN4", "P0.29_AIN5", "P0.30_AIN6", "P0.31_AIN7",
]
GPIO = ["P1.09", "P0.11", "P0.15", "P0.17", "P0.20"]
NRF_SRC = "ds:pa520104~pc5353448..pc5353451"

# JLC basic passives (verified get(kind='part') 2026-10-26)
C100N = "C307331"  # 100nF 50V X7R 0402
C1U = "C52923"  # 1uF 25V X5R 0402
C4U7 = "C19666"  # 4.7uF 16V X5R 0603
C10N = "C15195"  # 10nF 50V X7R 0402
C12P = "C1547"  # 12pF C0G 0402
C100P = "C1546"  # 100pF C0G 0402

# Hand placement, leaf frame (x, y, rot). Pin sides of U1 at rot 0 (catalog
# footprint C2895249): left 1-10 (DEC1, XL1, XL2, AIN2, AIN3, P1.09, P0.11,
# VDD, VDDH, VBUS, top to bottom), bottom 11-20 (.., VDD 18, SWD), right
# 21-30 (DEC5, NFC, ANT y=-0.6, VSS_PA, DEC6, DEC3, XC1, XC2, VDD 30), top
# 31-40 (AIN1..AIN7, VSS, DEC4, DCC, VDD 40).
POSE: dict[str, tuple[float, float, float]] = {
    # radio: U1 next to the antenna, the match chain runs +x from ANT
    "U1": (82.0, 0.0, 0.0),
    "L1": (86.0, -0.6, 0.0),
    "C3": (87.3, -2.1, 90.0),
    "L3": (88.6, -0.6, 0.0),
    "C4": (89.9, -2.1, 90.0),
    "C14": (89.9, 0.8, 90.0),
    "R14": (91.2, -0.6, 0.0),
    "C15": (92.5, -2.1, 90.0),
    "AE1": (95.5, -0.6, 0.0),
    # U1 support, each on its own side of the package
    "X1": (86.3, 1.9, 0.0),  # XC1/XC2, right side
    "C10": (85.6, 3.9, 0.0),  # DEC3
    "C7": (88.4, 2.6, 90.0),  # VDD pin 30
    "C8": (85.6, -3.6, 90.0),  # DEC5
    "C13": (83.0, -3.8, 0.0),  # VDD pin 18
    "C12": (80.8, 4.0, 0.0),  # DEC4, top
    "C11": (78.5, 3.8, 0.0),  # DEC1, top-left
    "X2": (77.2, 1.4, 90.0),  # XL1/XL2, left
    "C1": (75.4, 2.4, 0.0),
    "C2": (75.4, 0.4, 0.0),
    "C5": (77.6, -1.3, 90.0),  # VDD pin 8
    "C6": (65.8, 1.0, 0.0),  # VDD bulk, in the corridor (no room by U1)
    # harvester: U2 boost loop tight, dividers below it
    "U2": (82.0, -9.0, 0.0),
    "L2": (79.4, -5.6, 0.0),  # LBST (top-left) to VIN_DC (left)
    "C20": (78.9, -8.8, 90.0),  # CIN on VIN_DC
    "C23": (78.9, -11.0, 90.0),  # CREF on VREF_SAMP
    "C21": (82.0, -5.8, 0.0),  # CSTOR on VSTOR (top)
    "C22": (84.4, -5.8, 0.0),  # CBYP
    "R26": (85.6, -9.0, 90.0),  # OK divider, right side
    "R27": (86.8, -9.0, 90.0),
    "R28": (88.0, -9.0, 90.0),
    "R20": (80.6, -12.3, 0.0),  # VOC divider
    "R21": (82.4, -12.3, 0.0),
    "R22": (84.2, -12.3, 0.0),  # OV divider
    "R23": (86.0, -12.3, 0.0),
    "R24": (80.6, -13.7, 0.0),  # UV divider
    "R25": (82.4, -13.7, 0.0),
    # the two big parts, either side of the midrib corridor
    "PV1": (64.5, 7.5, 0.0),
    "BT1": (62.8, -9.0, 180.0),  # tabs toward U2
    # comb readout in the midrib corridor, close to U1's AIN side
    "R10": (70.0, 2.4, 0.0),
    "R11": (71.8, 2.4, 0.0),
    "C40": (73.6, 2.4, 0.0),
    "R12": (70.0, 0.4, 0.0),
    "R13": (71.8, 0.4, 0.0),
    "C41": (73.6, 0.4, 0.0),
    # RH/T sensor on the upper edge, away from the boost inductor
    "U3": (86.0, 8.5, 0.0),
    "C30": (86.0, 10.6, 0.0),
    "R30": (88.4, 8.5, 90.0),
    "R31": (89.6, 8.5, 90.0),
}
TESTPOINTS = {  # programming pads (program before coating)
    "VSTOR": (90.0, -4.6),
    "GND": (92.0, -4.6),
    "SWDIO": (90.0, -6.6),
    "SWDCLK": (92.0, -6.6),
    "NRESET": (91.0, -8.6),
}


def B(x: float, y: float) -> tuple[float, float]:
    """Leaf frame -> board frame."""
    return round(x + OX, 4), round(y + OY, 4)


def _coords(poly: Polygon) -> list[list[float]]:
    return [[round(x + OX, 3), round(y + OY, 3)] for x, y in poly.exterior.coords]


def slit_poly() -> Polygon:
    """Radial slit from inside the stalk hole to past the ring edge, through
    SLIT_EXIT (Reto 2026-10-26: closer to the leaf)."""
    cx, cy = RING_C
    ang = math.degrees(math.atan2(SLIT_EXIT[1] - cy, SLIT_EXIT[0] - cx))
    s = box(HOLE_R - 0.5, -SLIT_W / 2, RING_R + 1.0, SLIT_W / 2)
    return affinity.translate(affinity.rotate(s, ang, origin=(0, 0)), cx, cy)


def zip_discs(r: float) -> list[Polygon]:
    return [Point(x, y).buffer(r, 8) for x, y in ZIP_HOLES]


def geometry():
    """All in the leaf frame."""
    blade = leaf_outline(LEAF_L, LEAF_W)
    ring = Point(*RING_C).buffer(RING_R, 64)
    shape = blade.union(ring)
    # 1.5 mm fillet where the ring meets the leaf base
    shape = shape.buffer(1.5, join_style="round").buffer(-1.5, join_style="round")
    hole = Point(*RING_C).buffer(HOLE_R, 64)
    slit = slit_poly()
    # the hole joins the outline through the slit: one simply connected path
    shape = shape.difference(hole).difference(slit).simplify(0.02)
    ring_zone = ring.difference(hole).difference(slit).intersection(shape).simplify(0.05)
    return blade, shape, ring_zone


def supercap_footprint() -> pcb.Footprint:
    """CAP-XX DMT334 21 x 14 x 3.5 mm prismatic (pa520112 pc5354029).
    PROVISIONAL pads: the mechanical drawing is an image the ingest did not
    extract (gr520326) -- verify the tab positions before ordering."""
    return pcb.Footprint(
        "capxx_DMT334_provisional",
        [
            pcb.pad("+", "rect", -11.5, 3.5, w=3.0, h=4.0),
            pcb.pad("-", "rect", -11.5, -3.5, w=3.0, h=4.0),
        ],
        courtyard={"bbox": [-13.5, -7.5, 11.0, 7.5]},
        note="PROVISIONAL land pattern, verify vs pa520112 mechanical drawing",
    )


def build() -> pcb.Board:
    blade, shape, ring_zone = geometry()
    b = pcb.Board(
        SLUG,
        stackup=4,
        sides="top",
        meta={
            "slug": SLUG,
            "todo": "td485252",
            "dogfood": "td519926",
            "fab": "JLCPCB 4-layer flex, ENIG, 12 um outer copper (3/3 mil combs), ~0.3 mm",
            "frame": f"board = leaf frame + ({OX:g}, {OY:g}); leaf base x=0, midrib y=0",
        },
    )
    b.outline(_coords(shape), note="leaf + hanger ring; radial slit from the stalk hole")
    b.keepout(
        _coords(ring_zone),
        reason="hanger ring: PI stiffener, shears open over the stalk; no parts, no copper",
    )
    for (zx, zy), disc in zip(ZIP_HOLES, zip_discs(ZIP_KEEP), strict=True):
        b.hole(*B(zx, zy), drill=ZIP_D, ring=0.0, plated=False,
               note="zip-tie fallback mount (Reto 2026-10-26)")
        b.keepout(_coords(disc), reason="zip-tie hole: no copper, no parts")

    # -- nets: every net carries a conservative current and a voltage -----
    def net(name, i, v, **kw):
        return b.net(name, current=i, voltage=v, **kw)

    gnd = net("GND", 0.3, 0.0, cls="gnd")
    vstor = net("VSTOR", 0.05, 3.3, note="system rail = nRF VDD; TX peak ~15 mA")
    vbat = net("VBAT", 0.1, 3.3, note="supercap")
    vin = net("VIN_DC", 0.25, 2.1, note="solar +; carries the boost inductor current")
    lbst = net("LBST", 0.25, 3.3, note="boost switch node, BQ25504 peak ~230 mA")
    vrdiv = net("VRDIV", 0.001, 3.3)
    ov, uv = net("VBAT_OV", 0.001, 1.25), net("VBAT_UV", 0.001, 1.25)
    okp, okh = net("OK_PROG", 0.001, 1.25), net("OK_HYST", 0.001, 1.25)
    vbat_ok = net("VBAT_OK", 0.001, 3.3)
    voc, vref = net("VOC_SAMP", 0.001, 2.1), net("VREF_SAMP", 0.001, 2.1)
    dec1, dec3 = net("DEC1", 0.02, 1.1), net("DEC3", 0.02, 1.3)
    dec4, dec5 = net("DEC4", 0.02, 1.3), net("DEC5", 0.02, 1.3)
    xc1, xc2 = net("XC1", 0.001, 1.3), net("XC2", 0.001, 1.3)
    xl1, xl2 = net("XL1", 0.001, 1.3), net("XL2", 0.001, 1.3)
    ant = net("ANT", 0.03, 3.3, cls="rf", impedance=50)
    rf1 = net("RF1", 0.03, 3.3, cls="rf", impedance=50)
    rf2 = net("RF2", 0.03, 3.3, cls="rf", impedance=50)
    rf3 = net("RF_FEED", 0.03, 3.3, cls="rf", impedance=50)
    swdio, swdclk = net("SWDIO", 0.005, 3.3), net("SWDCLK", 0.005, 3.3)
    nreset = net("NRESET", 0.001, 3.3)
    sda, scl = net("SDA", 0.003, 3.3, cls="i2c"), net("SCL", 0.003, 3.3, cls="i2c")
    # S1 resistive readout: bipolar pulses, DC-blocked comb
    exa_lo, exa_hi = net("S1_EXA_LO", 0.005, 3.3), net("S1_EXA_HI", 0.001, 3.3)
    exb = net("S1_EXB", 0.005, 3.3)
    s1m = net("S1_M", 0.005, 3.3, cls="sense", note="SAADC node, high impedance")
    s1a = net("S1_A", 0.005, 3.3, cls="sense")
    # S2 capacitive readout: RC charge time vs a C0G reference, COMP
    cdrv = net("S2_DRV", 0.001, 3.3)
    s2a = net("S2_A", 0.001, 3.3, cls="sense", note="COMP node, high impedance, guard")
    s2ref = net("S2_REF", 0.001, 3.3, cls="sense")
    b.net_class("sense", note="hi-Z sensor node: short, guarded by GND, no vias in the blade")
    b.net_class("rf", note="50 ohm CPWG on F.Cu over In1 GND")
    b.plane(1, net=gnd)

    # -- U1 nRF52833 + RF chain -------------------------------------------
    u1 = b.part("U1", lcsc="C2895249", label="nRF52833-QDAA")
    u1.pin_functions(
        {**{p: ["GPIO", "AIN", "COMP"] for p in AIN}, **{p: ["GPIO"] for p in GPIO}},
        source=NRF_SRC,
    )
    for p in ("VDD", "VDDH"):
        u1.pin(
            p,
            etype="power_in",
            max_current_a=0.1,
            max_voltage_v=3.3,
            assume="VSTOR rail as declared on the net (0.03 A, 3.3 V); "
            "VDDH shorted to VDD in normal voltage mode (pc5349346)",
        )
    vstor.tie(u1["VDD"], u1["VDDH"], note="normal voltage mode: VDDH shorted to VDD (pc5349346)")
    for p in ("VSS", "VSS_PA", "VBUS"):
        u1.pin(
            p,
            etype="power_in",
            max_current_a=0.3,
            max_voltage_v=3.3,
            assume="GND rail return for the VSTOR domain as declared on "
            "the net; VBUS is the USB 5 V presence pin bounded by the "
            "harvester input",
        )
    gnd.tie(u1["VSS"], u1["VSS_PA"], u1["VBUS"])
    for p in ("DEC1", "DEC3", "DEC4", "DEC6", "DEC5"):
        u1.pin(
            p,
            etype="passive",
            fmax_hz=0.0,
            assume="internal regulator decoupling pin, DC (like a cap terminal)",
        )
    u1.pin("XC1", etype="passive", fmax_hz=32e6, assume="32 MHz crystal terminal")
    u1.pin("XC2", etype="passive", fmax_hz=32e6, assume="32 MHz crystal terminal")
    u1.pin(
        "P0.00_XL1", etype="passive", fmax_hz=32768.0,
        assume="32.768 kHz crystal terminal",
    )
    u1.pin(
        "P0.01_XL2", etype="passive", fmax_hz=32768.0,
        assume="32.768 kHz crystal terminal",
    )
    u1.pin(
        "ANT", etype="output", fmax_hz=2.4e9,
        assume="2.4 GHz RF output into the Nordic match chain",
    )
    u1.pin("SWDIO", etype="bidi", fmax_hz=4e6, assume="SWD debug data")
    u1.pin("SWDCLK", etype="input", fmax_hz=4e6, assume="SWD debug clock")
    u1.pin(
        "P0.18_nRESET", etype="input", fmax_hz=0.0,
        assume="reset line, explicit DC",
    )
    for n in ("DECUSB", "D-", "D+", "DCC", "P0.09_NFC1", "P0.10_NFC2"):
        u1.pin(n).nc()
    dec1.tie(u1["DEC1"])
    dec3.tie(u1["DEC3"])
    dec4.tie(u1["DEC4"], u1["DEC6"], note="DEC6 must connect to DEC4")
    dec5.tie(u1["DEC5"])
    xc1.tie(u1["XC1"])
    xc2.tie(u1["XC2"])
    xl1.tie(u1["P0.00_XL1"])
    xl2.tie(u1["P0.01_XL2"])
    ant.tie(u1["ANT"])
    swdio.tie(u1["SWDIO"])
    swdclk.tie(u1["SWDCLK"])
    nreset.tie(u1["P0.18_nRESET"])
    # solver-assigned pins (the firmware pin map is generated after place)
    for n_, name in ((s1m, "S1_M"), (s2a, "S2_A"), (s2ref, "S2_REF")):
        n_.tie(
            u1.pin(
                any_of=AIN, name=name, etype="analog_in", fmax_hz=1e3,
                assume="slow RC wetness-sense node",
            )
        )
    for n_, name, et, fm in (
        (exa_lo, "S1_EXA_LO", "output", 1e5),
        (exa_hi, "S1_EXA_HI", "output", 1e5),
        (exb, "S1_EXB", "output", 1e5),
        (cdrv, "S2_DRV", "output", 1e5),
        (sda, "SDA", "open_drain", 400e3),
        (scl, "SCL", "open_drain", 400e3),
        (vbat_ok, "VBAT_OK", "input", 1e3),
    ):
        n_.tie(
            u1.pin(
                any_of=GPIO + AIN, name=name, etype=et, fmax_hz=fm,
                assume="flex slot; bounds are conservative design estimates",
            )
        )

    def cap(ref, lcsc, value, a, k=gnd, near=None, within=2.0, note=None, fmax=0.0):
        c = b.part(ref, lcsc=lcsc, value=value, label=f"C {value}", note=note)
        if lcsc is None:
            c.declare("1", "2")
        for num in ("1", "2"):
            c.pin(num, etype="passive", fmax_hz=fmax, assume="cap terminal")
        a.tie(c["1"])
        k.tie(c["2"])
        if near is not None:
            b.near(c, near, within)
        return c

    def two_pin(ref, value, label, a, k, note=None, fmax=0.0):
        p = b.part(ref, value=value, label=label, note=note)
        p.declare("1", "2")
        for num in ("1", "2"):
            p.pin(num, etype="passive", fmax_hz=fmax, assume="two-pin terminal")
        a.tie(p["1"])
        k.tie(p["2"])
        return p

    def res(ref, value, a, k, note=None, fmax=0.0):
        return two_pin(ref, value, f"R {value} 0402 1%", a, k, note=note, fmax=fmax)

    # RF: Nordic match (L1, C3, L3, C4; topology inferred, VERIFY vs Fig 211)
    # then a pi pad for the chip antenna (R14 0R + C14/C15 DNP).
    two_pin("L1", "4.7nH", "L 4.7nH 0402 HF", ant, rf1, fmax=2.4e9)
    cap("C3", None, "1.0pF", rf1, fmax=2.4e9)
    two_pin("L3", "2.2nH", "L 2.2nH 0402 HF", rf1, rf2, fmax=2.4e9)
    cap("C4", None, "1.2pF", rf2, fmax=2.4e9)
    cap("C14", None, "DNP", rf2, note="antenna pi match, not fitted", fmax=2.4e9)
    res("R14", "0R", rf2, rf3, note="antenna match series element", fmax=2.4e9)
    cap("C15", None, "DNP", rf3, note="antenna pi match, not fitted", fmax=2.4e9)

    ae1 = b.part("AE1", lcsc="C49330090", label="BW2.4MNX3216 chip antenna")
    # the feed point radiates and receives — <> on the schematic
    ae1.pin(
        "ANT", etype="bidi", fmax_hz=2.4e9,
        assume="2.4 GHz antenna feed point",
    )
    rf3.tie(ae1["ANT"])
    ae1["2"].nc()
    tip = blade.intersection(box(ANT_KEEP_X, -LEAF_W, LEAF_L + 1, LEAF_W))
    b.keepout(
        _coords(tip),
        layers=[1, 2, 3],
        reason="chip antenna: no inner/bottom copper from the antenna to the tip",
    )

    # nRF52833 config 6 BOM (Table 94). Which cap sits on which pin is
    # inferred: Figure 211 (the schematic) was not extracted. VERIFY.
    cap("C5", C100N, "100nF", vstor, near=u1, note="VDD pin 8/9")
    cap("C7", C100N, "100nF", vstor, near=u1, note="VDD pin 30")
    cap("C11", C100N, "100nF", dec1, near=u1, note="DEC1")
    cap("C6", C4U7, "4.7uF", vstor, near=u1, within=4.0, note="VDD bulk, pin 40")
    cap("C13", C1U, "1uF", vstor, near=u1, note="VDD pin 18")
    cap("C8", None, "820pF", dec5, near=u1, note="DEC5")
    cap("C10", C100P, "100pF", dec3, near=u1, note="DEC3")
    cap("C12", C1U, "1uF", dec4, near=u1, note="DEC4/DEC6")
    x1 = b.part("X1", lcsc="C18213028", label="32MHz 1612 8pF")
    x1.pin("1", etype="passive", fmax_hz=32e6, assume="32 MHz crystal terminal")
    x1.pin("3", etype="passive", fmax_hz=32e6, assume="32 MHz crystal terminal")
    x1.pin(
        "GND", etype="power_in", max_current_a=0.3, max_voltage_v=3.3,
        assume="crystal ground return",
    )
    xc1.tie(x1["1"])
    xc2.tie(x1["3"])
    gnd.tie(x1["GND"])
    b.near(x1, u1, 3.0)
    x2 = b.part("X2", lcsc="C17179429", label="32.768kHz 2012 9pF")
    x2.pin("OSC1", etype="passive", fmax_hz=32768.0, assume="32.768 kHz crystal")
    x2.pin("OSC2", etype="passive", fmax_hz=32768.0, assume="32.768 kHz crystal")
    xl1.tie(x2["OSC1"])
    xl2.tie(x2["OSC2"])
    b.near(x2, u1, 4.0)
    cap("C1", C12P, "12pF", xl1, near=x2)
    cap("C2", C12P, "12pF", xl2, near=x2)

    # -- U2 BQ25504 harvester (da519848 9.2.1; thresholds by its equations) --
    u2 = b.part("U2", lcsc="C2876746", label="BQ25504RGTR")
    for p in ("VSS", "AVSS", "EP"):
        u2.pin(
            p, etype="power_in", max_current_a=0.3, max_voltage_v=3.3,
            assume="BQ25504 ground return (GND net carries 0.3 A)",
        )
    u2.pin(
        "VIN_DC", etype="power_in", max_current_a=0.3, max_voltage_v=5.1,
        assume="harvester input from PV; BQ25504 VIN_DC absolute maximum",
    )
    u2.pin(
        "VSTOR", etype="power_out", max_current_a=0.1, max_voltage_v=5.1,
        assume="boost storage rail; BQ25504 VSTOR range, board configures ~3.3 V",
    )
    u2.pin(
        "VBAT", etype="power_in", max_current_a=0.1, max_voltage_v=4.2,
        assume="supercap storage element, 4.2 V max",
    )
    u2.pin(
        "LBST", etype="power_out", max_current_a=0.3, max_voltage_v=5.1,
        assume="boost switch node",
    )
    u2.pin(
        "VRDIV", etype="analog_out", fmax_hz=0.0,
        assume="MPPT divider sense node, DC",
    )
    for p in ("VBAT_OV", "VBAT_UV", "OK_PROG", "OK_HYST", "VOC_SAMP"):
        u2.pin(
            p, etype="analog_in", fmax_hz=0.0,
            assume="threshold/programming sense pin, DC",
        )
    u2.pin(
        "VBAT_OK", etype="output", fmax_hz=1e3,
        assume="battery-ok status output",
    )
    u2.pin(
        "VREF_SAMP", etype="analog_out", fmax_hz=0.0,
        assume="sampling reference output, DC",
    )
    u2.pin(
        "OT_PROG", etype="passive", fmax_hz=0.0,
        assume="overtemperature program pin tied low",
    )
    gnd.tie(u2["VSS"], u2["AVSS"], u2["EP"])
    gnd.tie(u2["OT_PROG"], note="OT_PROG low: stop charging above 60 C")
    vin.tie(u2["VIN_DC"])
    lbst.tie(u2["LBST"])
    vstor.tie(u2["VSTOR"])
    vbat.tie(u2["VBAT"])
    vrdiv.tie(u2["VRDIV"])
    ov.tie(u2["VBAT_OV"])
    uv.tie(u2["VBAT_UV"])
    okp.tie(u2["OK_PROG"])
    okh.tie(u2["OK_HYST"])
    vbat_ok.tie(u2["VBAT_OK"])
    voc.tie(u2["VOC_SAMP"])
    vref.tie(u2["VREF_SAMP"])
    l2 = b.part("L2", lcsc="C49234183", label="22uH 0806 NR2016-220M")
    for num in ("1", "2"):
        l2.pin(
            num, etype="passive", fmax_hz=1e6,
            assume="boost inductor terminal, ~1 MHz switching",
        )
    lbst.tie(l2["1"])
    vin.tie(l2["2"])
    cap("C20", C4U7, "4.7uF", vin, note="CHVR, close to pins 2/1")
    cap("C21", C4U7, "4.7uF", vstor, note="CSTOR, close to pins 15/13")
    cap("C22", C10N, "10nF", vstor, note="CBYP")
    cap("C23", C10N, "10nF", vref, note="CREF, low leakage")
    # MPPT 78 % of Voc: ROC1 (bottom) 15.8M, ROC2 (top) 4.42M, sum 20.2M
    res("R20", "4.42M", vin, voc, note="ROC2")
    res("R21", "15.8M", voc, gnd, note="ROC1")
    # VBAT_OV = 1.5*1.25*(1+ROV2/ROV1) = 3.32 V (nRF VDD max 3.6 V)
    res("R22", "4.32M", vrdiv, ov, note="ROV2")
    res("R23", "5.62M", ov, gnd, note="ROV1")
    # VBAT_UV = 1.25*(1+RUV2/RUV1) = 2.24 V
    res("R24", "4.42M", vrdiv, uv, note="RUV2")
    res("R25", "5.6M", uv, gnd, note="RUV1")
    # VBAT_OK 2.44 V rising-hysteresis 2.85 V (datasheet example values)
    res("R26", "1.43M", vrdiv, okh, note="ROK3")
    res("R27", "4.22M", okh, okp, note="ROK2")
    res("R28", "4.42M", okp, gnd, note="ROK1")

    pv1 = b.part("PV1", lcsc="C5358484", label="KXOB25-05X3F solar (consign)")
    pv1.pin(
        "+", etype="power_out", max_current_a=0.3, max_voltage_v=5.1,
        assume="PV cell into the harvester input (VIN_DC carries 0.25 A)",
    )
    pv1.pin(
        "-", etype="power_in", max_current_a=0.3, max_voltage_v=5.1,
        assume="PV cell return into the GND net",
    )
    vin.tie(pv1["+"])
    gnd.tie(pv1["-"])
    bt1 = b.part(
        "BT1",
        footprint=supercap_footprint(),
        value="470mF 4.2V",
        label="CAP-XX DMT334R2S474M3DTA0 (consign)",
        height_mm=3.8,
    )
    bt1.pin(
        "+", etype="power_out", max_current_a=0.1, max_voltage_v=4.2,
        assume="supercap storage element, 4.2 V max",
    )
    bt1.pin(
        "-", etype="power_in", max_current_a=0.3, max_voltage_v=4.2,
        assume="supercap return into the GND net",
    )
    vbat.tie(bt1["+"])
    gnd.tie(bt1["-"])

    # -- U3 SHT40 ---------------------------------------------------------
    u3 = b.part("U3", lcsc="C2909890", label="SHT40-AD1B")
    u3.pin("SDA", etype="open_drain", fmax_hz=400e3, assume="I2C data")
    u3.pin("SCL", etype="open_drain", fmax_hz=400e3, assume="I2C clock")
    u3.pin(
        "VDD", etype="power_in", max_current_a=0.1, max_voltage_v=3.3,
        assume="SHT40 supply on VSTOR",
    )
    for p in ("VSS", "EP"):
        u3.pin(
            p, etype="power_in", max_current_a=0.3, max_voltage_v=3.3,
            assume="SHT40 ground return (GND net carries 0.3 A)",
        )
    sda.tie(u3["SDA"])
    scl.tie(u3["SCL"])
    vstor.tie(u3["VDD"])
    gnd.tie(u3["VSS"], u3["EP"])
    cap("C30", C100N, "100nF", vstor, near=u3)
    res("R30", "10k", sda, vstor, note="I2C pull-up")
    res("R31", "10k", scl, vstor, note="I2C pull-up")

    # -- wetness combs: one geometry, two finishes, next to the hanger ----
    spec = CombSpec()
    avoid = zip_discs(ZIP_KEEP)
    fp1, _, _ = comb_footprint(
        "taz_comb_s1_c", blade, spec, side=+1, mask="open",
        x_from=SENSE_X[0], x_to=SENSE_X[1], avoid=avoid,
        note="S1 resistive, ENIG exposed; tape before parylene",
    )
    fp2, _, _ = comb_footprint(
        "taz_comb_s2_c", blade, spec, side=-1, mask="covered",
        x_from=SENSE_X[0], x_to=SENSE_X[1], avoid=avoid,
        note="S2 capacitive, coverlay + parylene; no copper below",
    )
    sns1 = b.part("SNS1", footprint=fp1, label="wetness comb S1 (ENIG)")
    sns2 = b.part("SNS2", footprint=fp2, label="wetness comb S2 (coated)")
    sns1.pin(
        "A", etype="analog_in", fmax_hz=1e3,
        assume="S1 sense electrode, slow RC",
    )
    sns1.pin(
        "B", etype="output", fmax_hz=1e5,
        assume="S1 excitation drive",
    )
    sns2.pin(
        "A", etype="analog_in", fmax_hz=1e3,
        assume="S2 sense electrode, slow RC",
    )
    sns2.pin(
        "B", etype="power_in", max_current_a=0.3, max_voltage_v=3.3,
        assume="S2 guard electrode on the GND net",
    )
    sns1.at(*B(0.0, 0.0), 0.0, fixed="both")
    sns2.at(*B(0.0, 0.0), 0.0, fixed="both")
    s1a.tie(sns1["A"])
    exb.tie(sns1["B"])
    s2a.tie(sns2["A"])
    gnd.tie(sns2["B"])
    sense_half = blade.intersection(box(SENSE_X[0] - 1.0, -LEAF_W, SENSE_X[1] + 1.0, LEAF_W))
    b.keepout(
        _coords(sense_half),
        layers=[1, 2, 3],
        reason="no copper or bottom parts under either comb (same stack under S1 and S2)",
    )

    # S1: EXA_LO -R10 100k- M, EXA_HI -R11 10M- M; M -C40 1uF- S1_A; S1_B = EXB
    res("R10", "100k", exa_lo, s1m, note="S1 low-range reference")
    res("R11", "10M", exa_hi, s1m, note="S1 high-range reference")
    cap("C40", C1U, "1uF", s1m, k=s1a, note="DC block: no net DC through the comb")
    # S2: DRV -R12 1M- S2_A (comb), DRV -R13 1M- S2_REF -C41 47pF C0G- GND
    res("R12", "1M", cdrv, s2a, note="S2 charge resistor")
    res("R13", "1M", cdrv, s2ref, note="reference charge resistor, match R12")
    cap("C41", None, "47pF C0G", s2ref, note="S2 reference, C0G 1%")

    # -- programming pads (program before coating; Reto 2026-10-26) -------
    nets = {"VSTOR": vstor, "GND": gnd, "SWDIO": swdio, "SWDCLK": swdclk, "NRESET": nreset}
    for name, (tx, ty) in TESTPOINTS.items():
        b.testpoint(nets[name], *B(tx, ty))

    # -- schematic sheets: one per group (declarative only; rigid=False
    # stamps nothing — every persisted group is rigid, and a rigid sheet
    # would pin its fixed anchor's neighbours, gr5257xx) -------------------
    sheets = {
        "radio": ["U1", "L1", "C3", "L3", "C4", "C14", "R14", "C15", "AE1", "X1", "X2",
                  "C1", "C2", "C5", "C6", "C7", "C8", "C10", "C11", "C12", "C13"],
        "power": ["PV1", "BT1", "U2", "L2", "C20", "C21", "C22", "C23",
                  "R20", "R21", "R22", "R23", "R24", "R25", "R26", "R27", "R28"],
        "sensors": ["SNS1", "SNS2", "R10", "R11", "C40", "R12", "R13", "C41",
                    "U3", "C30", "R30", "R31"],
    }
    for name, refs in sheets.items():
        b.group(name, [b._parts[r] for r in refs], rigid=False)

    # -- hand placement: R*/C* passives unlocked in xy, rest fixed ------
    # legalize.py shoves overlapping courtyards apart into out/pose_legal.json
    legal = HERE / "out" / "pose_legal.json"
    pose = dict(POSE)
    if legal.exists():
        pose.update({k: tuple(v) for k, v in json.loads(legal.read_text(encoding="utf-8")).items()})
    # nudges after the first check of the legal poses (real courtyards of AE1
    # and the 0402 row are a hair bigger than legalize.py's estimates)
    for ref, (dx, dy) in {"C15": (-0.5, 0.0), "R10": (-0.2, 0.0), "R11": (-0.2, 0.0),
                          "C40": (-0.2, 0.0), "R12": (-0.2, 0.0), "R13": (-0.2, 0.0),
                          "C41": (-0.2, 0.0)}.items():
        px, py, rot = pose[ref]
        pose[ref] = (px + dx, py + dy, rot)
    for ref, (px, py, rot) in pose.items():
        # the anneal may slide a passive, never rotate it off the authored
        # quarter turn (fixed='rot' keeps xy free, rotation locked). The
        # RF row parts (L1/L3/X1/X2) are unlocked too — with the silkroom
        # margin in the objective the anneal spreads the tip cluster over
        # the leaf half's unused width instead of the script packing it.
        unlocked = re.match(r"^[RC]\d+$", ref) or ref in {"L1", "L3", "X1", "X2"}
        fixed = "rot" if unlocked else "both"
        b._parts[ref].at(*B(px, py), rot, fixed=fixed)

    # -- fab layers the builder has no method for yet (gr520329) ----------
    b.meta["fab_layers"] = {
        "coverlay": {
            "default": "covered",
            "openings": ["all part pads", "SNS1 (whole electrode A/B, ENIG)", "test pads"],
            "rules": "JLC flex: opening = pad + 0.1 mm/side, >=0.15 mm to traces, bridge >=0.5 mm",
        },
        "stiffener": [
            {"material": "PI 0.225 mm", "side": "bottom", "region": "hanger ring",
             "note": "springy: shears open over the stalk, snaps back"},
            {"material": "PI 0.225 mm", "side": "bottom",
             "region": "8 mm discs around both zip-tie holes", "note": "tear-out reinforcement"},
            {"material": "FR4 0.2 mm", "side": "bottom",
             "region": f"electronics, leaf x {ELEC_FROM_X:g}..{ANT_KEEP_X:g} (board +{OX:g})",
             "note": "parts area rigid; rounded ends so the bend spreads"},
        ],
        "tape_mask": {"before": "parylene",
                      "regions": ["SNS1 (leaf x 4..48, y>0)", "U3 SHT40 opening",
                                  "PV1 face", "test pads"]},
    }
    return b


def _translate_pts(pts: list, dx: float, dy: float) -> None:
    """Shift a [[x, y], ...] point list in place."""
    for p in pts:
        p[0], p[1] = p[0] + dx, p[1] + dy


def to_quadrant(batch: dict) -> dict:
    """Batch post-process: shift the whole board into the positive quadrant
    (gr520794 — pcb_apply wants every board coordinate x,y >= 0). The builder
    has no board-level origin and y-literals sit all over the script, so this
    runs on the batch dict itself: bbox of the outline's own geometry gives
    DX/DY, then every board-frame coordinate moves by it uniformly. Pads are
    part-local inside footprints and nets carry no coordinates: untouched."""
    outline = next(f for f in batch.get("features", []) if f["ftype"] == "outline")
    path = outline["geom"]["path"]
    dx = max(0.0, -min(p[0] for p in path))
    dy = max(0.0, -min(p[1] for p in path))
    if not dx and not dy:
        return batch
    for c in batch["components"]:
        if "x" in c:
            c["x"], c["y"] = c["x"] + dx, c["y"] + dy
    for f in batch.get("features", []):
        if "x" in f:  # mounting_hole, testpoint, fiducial
            f["x"], f["y"] = f["x"] + dx, f["y"] + dy
        for key in ("path", "poly", "coords"):  # outline/keepout geometry
            if key in f.get("geom", {}):
                _translate_pts(f["geom"][key], dx, dy)
    return batch


if __name__ == "__main__":
    board = build()
    out = HERE / "out"
    pcb.run(board, out)
    batch = to_quadrant(board.to_batch())
    o = next(f for f in batch["features"] if f["ftype"] == "outline")["geom"]["path"]
    min_x, min_y = min(p[0] for p in o), min(p[1] for p in o)
    assert min_x >= 0 and min_y >= 0, f"outline dips negative: ({min_x}, {min_y})"
    print(f"quadrant: bbox min (x,y) at ({min_x:.3f}, {min_y:.3f})")
    (out / "batch.json").write_text(
        json.dumps(batch, indent=1) + "\n", encoding="utf-8"
    )
    print((out / "check.txt").read_text(encoding="utf-8").splitlines()[0])

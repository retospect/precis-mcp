"""blinky_led — the smallest board that shows the whole construction
API: one authored footprint, one resistor, one LED, two nets on a
20×15 mm outline.

Authored, not generated; compare ``examples/boards/nano_usb_pd``
(~33 KB of ``scripts/pcb-batch-to-board`` pad lists) for what the
converter emits instead — every line here is the design.
"""

from __future__ import annotations

import precis_geom.pcb as pcb


def chip_0603() -> pcb.Footprint:
    """A two-pad 0603 land pattern, pin ``1`` at −x."""
    return pcb.Footprint(
        "CHIP-0603",
        [
            pcb.pad("1", "rect", -0.8, 0.0, w=0.9, h=1.0),
            pcb.pad("2", "rect", 0.8, 0.0, w=0.9, h=1.0),
        ],
    )


def build() -> pcb.Board:
    b = pcb.Board(
        "blinky_led", stackup="2l", outline=pcb.rect(20.0, 15.0, corner_mm=1.0)
    )
    fp = b.footprint(chip_0603())
    vin = b.net("VIN", cls="power", voltage=5.0)
    r1 = b.part("R1", value="330", footprint=fp, pins=["1", "2"]).at(6.0, 7.5)
    d1 = b.part("D1", label="LED", footprint=fp, pins=["1", "2"]).at(14.0, 7.5)
    r1["1"].etype("passive", fmax_hz=0.0, assume="resistor")
    r1["2"].etype("passive", fmax_hz=0.0, assume="resistor")
    d1["1"].etype("input", fmax_hz=0.0, assume="led anode")
    vin.tie(r1["1"])
    b.wire(r1["2"], d1["1"], name="LED_A")
    d1["2"].nc()  # cathode open — a connector would land here
    return b


if __name__ == "__main__":
    pcb.write_manifest(build(), "out")

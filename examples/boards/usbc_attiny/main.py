"""usbc-attiny — the spec's §Q1 board (pcb-workspace-code).

USB-C + ATtiny1616 on a 2-layer prototype stackup — the acceptance
board for the offline program: ``uv run main.py --place --route`` on a
laptop with no network must write gerbers for this board.

The spec's worked example resolves pin tables through
``pcb.catalog(lcsc)`` (read MCP); offline there is no catalog, so each
part carries its ``pins=`` declaration verbatim and every pad is a
synthesized landpattern bound — which ``check.txt`` reports as
NOT-CHECKED and the gerber writer marks as preview geometry.
"""

import precis_geom.pcb as pcb


def build() -> pcb.Board:
    b = pcb.Board(
        "usbc-attiny",
        profile="prototype",
        stackup="2l",
        outline=pcb.rect(30, 20, corner_mm=2),
    )
    for x, y in ((3, 3), (27, 17)):
        b.hole(x, y, drill=2.2, ring=4.0)

    gnd = b.net("GND", cls="ground", current=0.5, voltage=0.0)
    vbus = b.net("VBUS", cls="power", current=0.5, voltage=5.0)
    v33 = b.net("3V3", cls="power", current=0.3, voltage=3.3)
    b.net_class("usb", width_mm=0.2, clearance_mm=0.2)
    b.net_class("power", width_mm=0.4)

    # USB-C receptacle: 5.1 k CC pull-downs make it a UFP sink.
    j1 = b.part(
        "J1",
        lcsc="C165948",
        roles={"connector"},
        pins=["A1", "A4", "A5", "A9", "A12", "B1", "B4", "B5", "B9", "B12", "SHELL"],
    )
    vbus.tie(*j1.pins("A4", "A9", "B4", "B9"))
    gnd.tie(*j1.pins("A1", "A12", "B1", "B12", "SHELL"))
    for i, cc in ((1, "A5"), (2, "B5")):
        r = b.part(f"R{i}", lcsc="C25905", value="5.1k", pins=["1", "2"])
        b.wire(j1[cc], r["1"], name=f"CC{i}")
        gnd.tie(r["2"])
    j1.on_edge("left")
    j1.at(2.0, 10.0, rot=90, fixed="rot")  # x/y free along the edge

    u2 = b.part(
        "U2",
        lcsc="C82942",
        pins=["VIN", "CE", "VOUT", "GND"],
        note="ME6211 LDO",
    )
    cin = b.part("C1", lcsc="C15849", value="1u", pins=["1", "2"])
    cout = b.part("C2", lcsc="C15849", value="1u", pins=["1", "2"])
    vbus.tie(u2["VIN"], u2["CE"], cin["1"])
    v33.tie(u2["VOUT"], cout["1"])
    gnd.tie(u2["GND"], cin["2"], cout["2"])
    b.near(u2, cin, within_mm=2.0)
    b.near(u2, cout, within_mm=2.0)

    u1 = b.part(
        "U1",
        lcsc="C168022",
        roles={"mcu"},
        pins=["VDD", "GND", "PA0", "PA1", "PA2", "PA3", "PA7", "PB0", "PB1"],
        note="ATtiny1616-SNR, SOIC-20",
    )
    v33.tie(u1["VDD"])
    gnd.tie(u1["GND"])
    c3 = b.part("C3", lcsc="C1525", value="100n", pins=["1", "2"])
    v33.tie(c3["1"])
    gnd.tie(c3["2"])
    c3["1"].current_role("shunt")
    b.near(u1, c3, within_mm=1.5)

    j2 = b.part("J2", lcsc="C2337", label="UPDI 1x3", pins=["1", "2", "3"])
    b.wire(j2["1"], u1["PA0"], name="UPDI")
    v33.tie(j2["2"])
    gnd.tie(j2["3"])
    j2.on_edge("right")

    led = b.part("D1", lcsc="C2286", value="LED green", pins=["A", "K"])
    r3 = b.part("R3", lcsc="C25744", value="1k", pins=["1", "2"])
    b.wire(u1["PA7"], r3["1"], name="LED")
    b.wire(r3["2"], led["A"], name="LED_K")
    gnd.tie(led["K"])

    for pin in ("PA1", "PA2", "PA3", "PB0", "PB1"):
        u1[pin].unused()
    return b


if __name__ == "__main__":
    print(build().check().summary())
    pcb.run(build(), "out")

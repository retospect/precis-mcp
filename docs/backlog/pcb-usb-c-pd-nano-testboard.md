---
status: draft
title: Test board — USB-C PD Arduino Nano (5V) with switched high-side power outputs
prio: normal
model: opus
---

# Test board — USB-C PD Arduino Nano (5V) with switched power outputs

The first real board for the guided place+route system. Filed to be built
**after** the system gaps in §Blockers are closed — it is deliberately
chosen to exercise them (multi-layer power routing, per-net width and
clearance, thermal copper), not to dodge them.

## Intent

An Arduino-Nano-shaped 5V board that sinks USB-C Power Delivery, exposes
the negotiated rails, and switches them to external loads through
open-drain low-side FETs, with LED indication of both rail presence and
negotiation failure.

## Design corrections to fold in before drawing anything

Three things in the original sketch do not survive contact with the USB PD
spec. They change the topology, so they are recorded here rather than
discovered mid-layout.

**12 V is not a standard PD fixed PDO.** The normative fixed voltages are
**5 V, 9 V, 15 V, 20 V**. 12 V is an *optional* PDO — some sources offer
it, many do not, and none are required to. If 12 V is genuinely wanted,
either derive it from a higher rail with a buck, or request it via **PPS**
(programmable, ~3.3–21 V in 20 mV steps) and accept that PPS-capable
sources are a subset. Do not design assuming a 12 V contract is available.

**One PD contract yields one VBUS voltage at a time.** A sink cannot hold
20 V, 12 V and 5 V simultaneously from a single negotiation. The board must
either negotiate the highest rail (20 V) and **derive the lower rails with
bucks**, or switch contracts sequentially and give up simultaneity. The
buck approach is assumed below.

**5 A requires an e-marked cable, and telling "cable can't" from "source
can't" is subtler than it looks.** Full e-marker interrogation needs SOP'
VDM communication (an FUSB302-class PHY plus a PD stack) — which does not
fit comfortably on an ATmega328P. There is a cheaper discrimination that
does work: a source only advertises 5 A PDOs when an e-marked cable is
attached, so the sink can read the *advertised source capabilities* and
infer:

| Advertised | Inference |
|---|---|
| 20 V/5 A present | cable + source both fine |
| 20 V/3 A but no 5 A | cable is the limit (not e-marked) |
| no 20 V at all | source is the limit |

That is enough to drive two distinct error LEDs with an autonomous sink
controller, and avoids a full PD stack.

## Topology

- **USB-C receptacle** + CC handling. Sink controller candidates:
  **STUSB4500** (autonomous, I²C status + PDO readback — preferred, because
  the error-LED requirement needs the negotiation result) or **CH224K**
  (cheapest, minimal feedback) or **FUSB302** (full stack, only if e-marker
  interrogation is later wanted). Chosen for readback: STUSB4500.
- **Negotiate 20 V.** VBUS feeds a wide-Vin buck chain. Rails follow the
  PD fixed set rather than the original 5/12/20 sketch:
  - **20 V** = VBUS direct (post-negotiation)
  - **15 V** = buck from VBUS
  - **9 V** = buck from VBUS
  - **5 V** = buck from VBUS; this rail also powers the ATmega328P. The MCU
    cannot run from vSafe5V once VBUS moves to 20 V, so the 5 V buck is
    load-bearing, not a convenience.

  Four rails means three bucks, which is a real area cost (see §Open
  questions). Populating a subset — say 20 V and 5 V only — is a sane first
  build; the footprints can stay on the board unstuffed.
- **Open-drain outputs**: logic-level N-channel FETs, low-side, each with a
  **gate pulldown** (~100 k) so outputs are guaranteed off during MCU reset
  and while the pin floats. Rated for 20 V / 5 A ⇒ 30 V+ FET, low R_DS(on),
  in a package that can actually carry 5 A (DPAK / PowerPAK class, not
  SOT-23) with thermal copper. Decide flyback protection once the intended
  loads are known — inductive loads need it.
- **LEDs**: one per rail actually present (5 V, 9 V, 15 V, 20 V), plus
  two error indicators driven off the PDO-inspection table above
  ("cable not 5 A-capable", "source lacks requested PDO").

## Open questions

- Are the outputs driving inductive loads? Decides flyback diodes.
- How many of the four rails get stuffed on the first build? Three bucks is
  a lot of area for a Nano outline; 20 V + 5 V may be the honest v1.
- If 12 V specifically is still wanted (it is not a negotiable PDO), it has
  to come from a buck like the others, or from PPS on a source that offers
  it.
- **Form factor is a real risk**: a stock Nano is 18 × 45 mm. USB-C PD +
  up to three bucks + 5 A FETs + thermal copper is unlikely to fit that
  outline. Expect to either grow the board or accept 4 layers, and decide
  which deliberately.

## Blockers — system gaps this board would hit

Verified against the code, not assumed:

1. **No via geometry is realized.** `realize.py`'s `RealizeResult` carries
   only tracks; no `ctype='via'` copper is ever persisted. A 4-layer power
   board cannot actually change layers in realized copper. Blocks any
   multi-layer version of this board.
2. **Track width is a flat 0.25 mm default** (`realize.py`, `track_width_mm`
   — its own comment notes the real per-net width is not wired in). The
   cost function has an IPC-2221-style `thermal_rise` term, but it only
   *penalizes* overload; nothing ever *widens* copper. 5 A on 0.25 mm is a
   fuse. Blocks every high-current net here.
3. **`pcb_net_classes.rules` has no consumer** — `drc.py` reads only the fab
   capability table. So there is no per-net clearance for the 20 V nets and
   no per-net width. `op='class_rules'` round-trips but changes nothing.
4. **Slice 9 (JLCPCB ordering) is unbuilt**, blocked on the console scope
   grant — so the board could be designed and exported but not ordered
   through the system.

(1)–(3) are the same shape: the optimizer reasons about current and class,
and the realizer then ignores it. Closing them is the prerequisite for this
board being a real test rather than a drawing.

## Not blockers, but worth knowing

- There is **no ERC** — the netlist is authored directly at L0, so a wrong
  net is a wrong board with nothing to catch it. Review the netlist by hand.
- Via DRC rules exist and are correct but never fire (no via geometry) —
  a clean DRC on this board would not mean vias were checked.

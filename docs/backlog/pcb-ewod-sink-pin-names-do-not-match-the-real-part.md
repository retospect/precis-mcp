---
status: draft
title: the EWOD sink's declared pin names match no pad on the real C639448, so 56 of 59 pads are synthesized bounds
prio: high
pillar: 3d-design
---

# the EWOD sink's declared pin names match no pad on the real C639448

## The defect

`view='footprints'` on `ewod-dogfood-3` reports:

```
ARR1_SINK_0  C639448  cached: yes  easyeda:packageDetail:8108d701…  80 pads  56/59 pins
```

That last column is **56 of 59 pins synthesized** — i.e. 56 of the sink's
pads are not the real part's lands at all. They are default bounds from
`precis.pcb.landpattern`, which the module docstring is explicit about:
"`synthesized=True` from this module always means *dimensionally a bound*".
A board realized this way cannot be assembled: the pads are at a generic
pitch in generic positions, not the part's land pattern.

The cache is not the problem. The prod `part_footprints` row for C639448 is
a real EasyEDA fetch — 80 `RECT` pads, **2.0 × 0.5 mm at 0.8 mm pitch**,
courtyard 19 × 23.5 mm. A correct PQFP-80 land pattern.

## Root cause: the names don't join

`part_footprints['C639448'].pin_map` names the pads:

```
HVOUT1 … HVOUT64, VPP (×2, pins 25 and 40), DIOA, DIOB, CLK, LE,
POL, BL, DIR, VDD, GND, HVGND, N/C (×4)
```

The dogfood/prod design's `sink_grid` params declare:

```
channel_pins:   OUT0 … OUT63
serial_in_pin:  DIN
serial_out_pin: DOUT
top_plate_pin:  VPP
power:          {VDD: VDD_LOGIC, GND: GND}
```

Only `VPP`, `VDD`, `GND` exist on the real part. 59 − 3 = **56**, which is
exactly the synthesized count. The channel pins should be `HVOUT1…HVOUT64`
(1-based, not 0-based) and the serial pair should be `DIOA` / `DIOB`.

So the merge at `pcb_realize.pad_geometry` joins by pin NAME, finds no pad
for 56 of them, and silently falls back to a synthesized bound — per pin,
which is why the cache row still reads `cached: yes`.

## Why it was not caught

`gr346009` observed the same arithmetic ("dogfood-1 read `synthesized: no`
with 56 synthesized pins") and was closed by fixing the **reporting** — the
`footprints` view now counts per pin rather than per cache row (see the
comment above the `synth_by_refdes` tally in `precis.handlers.pcb`'s
footprints view). The display was corrected; the pin names were not. The
gripe is soft-deleted, so nothing tracks the underlying defect.

`check_synthesized_footprint` (`precis.pcb.drc`) does flag synthesized pads
carrying a `part_lcsc` as `error`, and `export_fab` refuses a gerber export
over synthesized geometry — so the fab gate is intact. The hole is that a
board can be generated, placed, routed and reviewed in this state without
the mismatch being the headline.

## Blast radius

Every EWOD board generated from these params, which includes the live
`pb345846` and `ewod-dogfood-1/2/3`. `U_TEMP` is a **different and worse defect** — see below.

Consequence for the campaign: **every escape-yield and congestion number
measured on these boards was measured against synthesized pad positions,
not the real land pattern.** Treat those numbers the way the
`_qfp_ring_footprint` docstring already treats the pre-2026-09-26 grid
numbers — as measured against an artifact.

## Confirmed by experiment (2026-09-30)

`ewod-dogfood-4` was created on prod with the *only* change being the sink
pin names — `OUT0…OUT63` → `HVOUT1…HVOUT64`, `DIN`/`DOUT` → `DIOA`/`DIOB`,
everything else byte-identical to `ewod-dogfood-3`. Result:

```
ARR1_SINK_0  C639448  cached: yes  80 pads  synthesized: no
```

56-of-59 synthesized → **zero**. The pin names were the whole cause; there
is nothing else to find on the sink.

## U_TEMP is not a naming bug — it is the wrong part

`C32254`'s cached footprint has six pads named `S1, D12, S2, G2, D12, G1`.
That is a **dual MOSFET**, not an I²C temperature sensor. The design
declares `U_TEMP` with `footprint: "SOIC-8"` and pins `VDD/GND/SCL/SDA`,
and its own label already says "LM75-class, **placeholder C-number**".

So no renaming fixes this: the C-number needs replacing with a real
LM75-class I²C sensor, which is a parts-selection call (package,
availability, price) and not something to guess at. Until then `U_TEMP`
will keep reading `4/4 pins` synthesized, and the `synthesized_footprint`
DRC error on it is correct rather than noise.

### RESOLVED 2026-09-30 — the part is `C28927` (TI TMP112AIDRLR)

Reto delegated the selection ("you find it. tmp112 or something?"), so this
is no longer blocked on him. `C28927` = Texas Instruments **TMP112AIDRLR**,
SOT-563, I2C digital temperature sensor, -40..+125 C, 1.4-3.6 V, ±0.5 C,
13-bit, JLCPCB-assemblable.

**Verified against the real footprint before being written down**, which is
the whole point of this item — a guessed C-number is exactly how `C32254`
got here. Fetched through the production path
(`precis.pcb.easyeda.fetch_component` -> `parse_component`) and it returns a
real 6-pad footprint, `source easyeda:packageDetail:12ec04a02cb54b98af05fc74f16c8bc5`,
courtyard bbox `[-0.9, -0.8, 0.9, 0.8]`, pad 5 at `w=0.3 h=0.6 rot=90`.

**Declare these pin names, taken from the footprint's own `pin_map`, not
from a datasheet reading:**

| pad | name |
| --- | --- |
| 1 | `SCL` |
| 2 | `GND` |
| 3 | `ALERT` |
| 4 | `ADD0` |
| 5 | `V+` |
| 6 | `SDA` |

Two traps in that table. The supply pin is **`V+`**, not `VDD` — the old
declaration's `VDD` would silently synthesize. And there is no `VDD`/`NC`
pair to drop: SOT-563 is 6 pins, so the `SOIC-8` footprint string in the
current design is wrong too and must go. `ALERT` and `ADD0` are real pins
that the design does not currently wire; leave them unconnected
deliberately (or tie `ADD0` for the I2C address) rather than omitting them
from the declaration.

**Blocker found while doing this, filed separately (gr458878):** the prod
`parts` catalog is **empty — 0 rows**. `precis pcb refresh-parts` has never
populated it, so `search(kind='part', ...)` finds nothing and no C-number
can be validated against price, stock or assemblability in-system. This
selection was verified through the EasyEDA footprint path and a public
parts lookup instead. Anything that wants to *choose* a part rather than
confirm a named one is blocked until the catalog is refreshed.

## In scope

- Correct the `sink_grid` pin names in the EWOD design to the real part's
  (`HVOUT1…HVOUT64`, `DIOA`, `DIOB`), keeping `VPP`/`VDD`/`GND`.
- Replace `U_TEMP`'s placeholder C-number with `C28927` (TMP112AIDRLR),
  declaring the six pin names in the table above — `V+`, not `VDD` — and
  dropping the wrong `SOIC-8` footprint string. Selection made 2026-09-30;
  no longer a Reto decision.
- Make the mismatch loud at generate time, not only at fab-export time: if
  a named pin has no pad on a *cached* footprint, that is a name error, not
  a missing footprint, and it should say so with both names in hand.

## Explicitly NOT in scope

- Changing how `landpattern` synthesizes. The fallback is correct
  behaviour for a part with no cached footprint; the bug is reaching it
  when a perfectly good footprint is cached.
- Regenerating `pb345846`. Destructive prod write; Reto's per-write
  go-ahead, separately.

## Acceptance

- `view='footprints'` on a freshly generated EWOD board reads
  `synthesized: no` for `ARR1_SINK_0` — already true on `ewod-dogfood-4`.
  `U_TEMP` follows only once a real sensor C-number is chosen.
- A test that asserts every declared `sink_grid` pin name resolves to a pad
  on the cached footprint — with a negative control that misnames one pin
  and confirms the check fires. Without the control the assertion is
  vacuous, which is how this survived once already.

Owner anchors: `precis.pcb.generators` (`sink_grid`),
`precis.pcb.realize.pad_geometry` (the name join), `precis.pcb.landpattern`
(the fallback).

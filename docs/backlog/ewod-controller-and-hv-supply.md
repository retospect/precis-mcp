---
status: in-progress
pillar: 3d-design
title: EWOD dogfood board — Arduino Pro Mini controller, USB-C power + programming, on-board 250 V flyback supply from the 5 V rail (rulings 12–22) + two prod-review defects
prio: high
---

Status 2026-10-17: slices 1-4 shipped; slices 5-6 wait on footprint intake after r44; slice 7 follows 5-6.

# EWOD dogfood board — controller, USB-C power, on-board 250 V supply (rulings 12–22) + two prod-review defects

> **Ruling 22 (Reto, approved ~20:10Z 2026-10-16) drops the 20 V USB-PD
> contract.** The board is a default-5 V Type-C sink: no CH224K, no buck,
> VBUS → PTC → optional load switch → the 5 V rail. Every 20 V, PD, CH224K
> or buck passage below is **superseded by ruling 22** (see that ruling at
> the end of Ruling 21's section). Ruling 21 (approved ~18:10Z) already moved
> the 250 V flyback onto the 5 V rail; it is **off by default** (the Mini
> enables and kills it).

Companion to `pcb-ewod-multitile.md` (the array generator; rulings 1–11
live there). This item holds rulings **12–15** (Reto, 2026-09-24),
rulings **16–19** (Reto, 2026-10-16) and the
two defects Reto found reviewing `ewod-dogfood-6` in the prod web view the
same day. Ruling 12 **supersedes ruling 9** (Teensy 4.0 + 74HCT245); ruling
14 **supersedes design-review item 6** (external boost + HV-in connector).

## Read first — nothing on the prod page is actionable yet

The prod DRC tally Reto pasted (`clearance 64 · connectivity 61 ·
silk_missing 7 · synthesized_footprint 1 · unrouted 59 · via_pad_keepout
2`) and the J_INSTR symptom ("wires go to SMT pads on the bottom but never
connect — wires only on top, no via") are the **pre-fix render**. The
cluster runs 8e0099fc, which predates every route fix on `main`:
`unrouted 59` is exactly the pre-fix count `pcb-ewod-multitile.md` records,
and the offline rebuild at current `main` routes 27–30 of 57 fabric nets.
The J_INSTR symptom is the layer-blind-router signature gr346744, fixed on
`main` by 0600f679 — `pcb-ewod-multitile.md` already records the same
symptom at the sink and U_TEMP; J_INSTR is a third instance, not a new
bug. **No number on the prod page means anything until a deploy plus a
re-run of `op='route'`.** If J_INSTR still ends over a bottom pad with no
via after that, it IS a new defect — file it then, not now. The last
campaign burned several rounds chasing numbers produced by stale code.

## Prerequisites (existing defects, not this item's work)

- **gr347037** is the open routing defect. Re-measured 2026-10-11 on
  ewod-dogfood-6: 10 of 58 nets fail (7 `no_path`, 3 `congestion`), down
  from the original 30 of 57. The radial breakout stubs are gr347037's own
  proposed remedy; the fix plan lives in `pcb-lazy-netlist-and-checks.md`.
- **gr346004** (2 `via_pad_keepout`: merged reservoir pad vs plaza ring
  vias) predates the 2.25 mm pitch change (ruling 10). Same rule:
  re-measure at HEAD first.
- Deploy of current `main` — every acceptance criterion below that says
  "on prod" is gated on it.

## Motivation / why

`ewod-dogfood-6` (retired 2026-10-26; regenerate to re-apply) was an array + sink + temp sensor with no controller,
no protected power entry, and an external HV supply it
was never going to get. Ruling 9 chose a 3.3 V controller that then needed
a level shifter; a 5 V controller deletes that part. A single USB-C port
that programs and powers the board (5 V, ruling 22) removes a second
connector. And two things the generator emits are wrong or
missing in a way that blinds DRC/router (Defect A) or leaves the top plate
unconnectable (Defect B).

## Ruling 12 — controller is an Arduino Pro Mini, 5 V / 16 MHz

Supersedes ruling 9. HV507 VDD is 4.5–5.5 V and `V_IH = VDD − 0.9 V`
(≥ 4.1 V at 5 V; Microchip DS20005845A — `SOURCE NEEDED`: import the
datasheet as a `datasheet` ref, no precis id exists in the specs). A 5 V
ATmega328P drives DIOA/CLK/LE/BL/POL directly. **Reclaimed: `U_LVL`
(74HCT245) is deleted; one 5 V rail serves the Mini, HV507 VDD and the
USB-serial bridge.** HV507 DIOB → Mini (read-back, optional) is 5 V into a
5 V pin — no buffer either way.

**Footprint intake** — a module, not a catalogued part, so a LOCAL
footprint (`footprints` block on `put(kind='pcb')`, the same path ruling 9
planned for the Teensy). Working numbers, all `SOURCE NEEDED` against the
vendor drawing before the footprint lands: body 33 × 18 mm; 2 × 12
through-hole pins at 2.54 mm along the long edges; a 6-pin programming
header (DTR/TXO/RXI/VCC/CTS/GND order, from the vendor .brd nets,
web:492682) on one short edge; 4 inner pads A4–A7. Intake done
2026-10-16: `SPARKFUN_PRO_MINI_328_5V` (34 pads, 17.78 × 33.02 mm) is on
`ewod-dogfood-6`, citing web:492682 (.brd) and web:492681 (schematic PDF,
empty text extraction). VERIFY on the unit actually bought: the TXO/RXI
silkscreen order on P2/P3 and the A4–A7 positions (clones differ).

**`sink_grid` control pins** — today only `serial_in_pin`/`serial_out_pin`
are parametrised (`generators.py::_SinkGrid`); the HV507 also has CLK, LE,
BL, POL and DIR (DIR fixes the shift direction, so DIOA=in/DIOB=out is a
wiring choice the config must state, not assume). Slice 2 adds them as
named config with an externally-facing net per control line
(`{name}_clk`, `{name}_le`, …, same convention as `{name}_serial_in`) so a
board-level `connections` entry wires the Mini to them; DIR and BL that
are tied rather than driven go through the existing `power` map (DIR →
GND or VDD, BL → VDD). `HVGND` is a `power` entry too; `C` (VERIFY its
function on the datasheet) is declared NC by Slice 1.

**Adequacy** — SPI at f_osc/2 = 8 MHz clocks a 64-bit HV507 frame in 8 µs;
EWOD refresh is ≤ 1 kHz (complement AC drive, design-review item 1) →
~100× headroom; a frame is 8 bytes against 2 KB SRAM. **Known ceiling, not
a blocker**: the 9×9 / 1024-pad multitile roadmap (two or more chained
HV507s, 128+ bits per frame, capacitive sensing on an ADC line) is where
the Teensy argument returns. Record it; do not act on it.

~~**TRAP — RAW pin.** The Pro Mini's RAW input feeds an on-board LDO rated
well below 20 V; with PD at 20 V, VBUS must never reach RAW.~~
**Removed 2026-10-16 by ruling 22:** VBUS is 5 V, so the 20 V trap is gone.
The Mini's VCC pin is fed from the 5 V rail and RAW is left unconnected by
convention.

## Ruling 13 — USB-C: power, PD negotiation to 20 V, and programming

> **Superseded 2026-10-16 by ruling 22 (PD dropped).** The 20 V contract,
> the CH224K, the PD VBUS divider and LED, the wide-Vin buck and the
> bring-up sequencing below are history. What survives: one USB-C port for
> power and programming, the CH340N bridge on the 5 V rail, and the
> DTR auto-reset wiring. CC1/CC2 are now bare 5.1 kΩ pull-downs.

Reto: "can we pd and get 20 V and would it be easier" → **yes for the HV
stage, roughly a wash on part count; do it.**

- **Why 20 V helps the HV stage** *(superseded 2026-10-16 by ruling 21:
  the HV stage runs from the 5 V rail; the argument is retained as history
  and no longer justifies the 20 V contract)* — see ruling 14: a Cockcroft-Walton
  ladder's output is `N × Vpp` of its drive, so 20 V in is N = 7 stages
  where 5 V in is N = 25+; for the rejected flyback it is the turns ratio
  (~1:5 vs ~1:20). At ~1 W and ~75 % efficiency the average input current
  falls from ~270 mA (5 V) to ~67 mA (20 V), so the whole primary side
  shrinks.
- **Cost of 20 V** — a PD sink controller plus a wide-Vin buck (20 → 5 V)
  for the Mini, HV507 VDD and the USB-serial bridge. Net: two jellybean
  ICs bought, one level shifter and one HV connector sold.
- **PD sink controller: CH224K** (fixed PDO, voltage selected on its CFG
  pins by resistor/strap, no I2C) + a VBUS resistor divider into one Mini
  ADC pin so firmware tells "20 V contract" from "vSafe5V only" and lights
  a PD-failed LED. **STUSB4500** is the alternative if I2C PDO read-back is
  later wanted. `pcb-usb-c-pd-nano-testboard.md` already surveys STUSB4500
  / CH224K / FUSB302 and the PDO-inference table — cite it, do not repeat
  it. Normative fixed PDOs are 5/9/15/20 V (12 V optional) — recorded there
  too. A 20 V PDO is only offered by higher-power sources (`SOURCE NEEDED`:
  USB PD power rules — import the spec section or a secondary); a 5/9/15 V
  source leaves the board at vSafe5V with the PD-failed LED lit, which is
  the designed failure mode.
- **CC1/CC2 are the PD controller's job** (not bare 5.1 kΩ Rd — that is
  the power-only design). Plus VBUS bulk capacitance and an ESD array on
  CC/D+/D−/VBUS.
- **Bring-up sequencing.** On plug-in VBUS is vSafe5V and rises to 20 V
  only after the contract. The buck is wide-Vin (5–20 V) so the Mini is
  alive before the contract; at Vin ≈ 5 V a buck cannot make 5.0 V —
  choose one that passes through at ~100 % duty (output ~4.7–4.9 V, inside
  HV507's 4.5 V floor and the ATmega328P's 16 MHz operating region;
  `SOURCE NEEDED`: ATmega328P safe-operating-area figure). ~~**HV stage:
  recommend letting it run wide-Vin**, no inhibit — a CW ladder's output
  scales linearly with its drive rail, so at 5 V the ladder sits at ~70 V,
  harmless; firmware reads the HV feedback divider (ruling 14) on a second
  ADC pin to know when the rail is up. Alternative (inhibit until the
  contract is confirmed) is one gate on the bridge driver's enable — keep
  the enable line on the schematic, default-on, so the alternative is a
  firmware choice later.~~ **Superseded 2026-10-16 by ruling 21:** the HV
  stage is OFF by default (FA/SD pulled up) and the Mini enables and kills
  it; it never runs wide-Vin.
- **The USB-serial bridge (CH340N, ruling 17) is powered from the 5 V
  rail.** ~~Reference designs tie bridge VCC to VBUS; here VBUS is 20 V after
  negotiation and destroys the bridge.~~ *(Superseded by ruling 22: VBUS is
  5 V; the rail still comes through the PTC.)* D+/D− are
  unaffected by the contract (PD runs on CC). The bridge's DTR goes to the
  Mini's DTR/RESET through the standard 100 nF cap for auto-reset upload;
  bridge TX/RX/DTR land on the Mini's 6-pin programming header pins, which
  the on-board bridge replaces.

## Ruling 14 — on-board 250 V supply: Cockcroft-Walton ladder from the 20 V rail

> *All 20 V figures here are also superseded by ruling 22.*

> **SUPERSEDED 2026-09-24 by ruling 15 — the topology is a FLYBACK.**
> Reto, on being shown the two facts that emerged after ruling 14 was
> made: "I like the flyback then." Everything below about stage counts,
> droop and the ladder's component ratings is retained as the rejected
> alternative's analysis, not as the build. The spacing, feedback-divider
> and bleeder constraints it derives still apply to the flyback's own
> 250 V output net — read those parts forward.

## Ruling 15 — the 250 V supply is a flyback from the 20 V PD rail

> **Superseded by ruling 22:** there is no 20 V PD rail; VIN is the 5 V rail.

> **Input rail superseded 2026-10-16 by ruling 21:** the flyback topology
> stands, but VIN is the **5 V buck rail**, not the 20 V PD rail (reason
> 2 below, and the ~1:5 ratio, are history). The ~1:20 transformer is the
> part that was chosen anyway (ruling 17). "Standalone, no MCU" is also
> superseded: the stage is off by default and the Mini enables/kills it.

**RULED 2026-09-24 (Reto).** Two facts that post-date ruling 14 reversed
it, and neither was available when the ladder was chosen:

1. **The ladder's regulation lever has no authority at our load.** A
   square-driven ladder settles at `N·Vpp` regardless of frequency or
   duty at sub-mA draw — the flip side of its negligible droop. So it
   needs *added* circuitry to regulate at all (hysteretic clock gating,
   or a linear HV post-regulator), which spends most of the simplicity
   that justified it.
2. **The 20 V PD rail (ruling 13) demoted the magnetic from custom to
   catalogue.** The ladder's central argument was avoiding a ~1:20
   transformer at 5 V in — genuinely the riskiest part on the board. At
   20 V in the ratio is ~1:5, which is an off-the-shelf coupled
   inductor / flyback transformer at sub-watt, not a custom winding.

So the thing the ladder existed to avoid stopped being hard, while the
ladder itself got harder. The flyback also brings closed-loop regulation
for free, and drops the part count from ~35–55 (bridge + gate driver +
oscillator + 28–49 passives + regulation circuit) to roughly 15.

**Retained from ruling 14, unchanged** — these are properties of a 250 V
net, not of the topology that made it:

- Output diode ≥ 400 V ultrafast; output capacitance rated ≥ 400 V, and
  high-voltage ceramics lose most of their capacitance to DC bias, so a
  film cap or a properly derated bank is the honest choice. (The flyback
  DOES need the 400 V-rated output parts the ladder avoided — that is
  the price being paid here, recorded deliberately.)
- The feedback divider's top leg must be 3–4 resistors **in series**: a
  ~10 MΩ leg burns only ~25 µA / ~6 mW at 250 V, but a single 0603 is
  often rated just 50–75 V working voltage.
- A bleeder resistor, as the safety discharge path for the 250 V bank.
  Note the duty is simpler than the ladder's — one output bank to drain,
  not a distributed column — but a flyback in DCM *can* run away at zero
  load, so the bleeder is also the minimum load the loop needs. Both
  duties, one part.
- The 250 V net inherits the IPC-2221B B4 class from rulings 3/10, and
  that is **blocked** on per-net clearance reaching the router — see the
  single-clearance maze-grid blocker in
  `pcb-lazy-netlist-and-checks.md`.
- HV507 is a 300 V-class part, so 250 V keeps margin.

**Dropped with the ladder:** the full bridge, its gate driver, the
oscillator, the stage-count derivation, and the per-stage
`working_voltage_v` refinement (the ladder's low stages could have run
at 0.13 mm; a flyback has one HV net at one voltage, so the win no
longer applies here — it remains a real lever for any future ladder).

**Correction first (decisive).** Reto asked whether "5 doublings" is
enough, which implies ×2 per stage (×32). A passive diode-capacitor ladder
does **not** compound: N stages give `Vout ≈ N × Vpp` (equivalently
`2·N·Vpeak`), **linear** in N. True ×2-per-stage compounding would need
every stage to re-chop its own DC output — N independent oscillators,
which nobody builds. 5 stages is not enough; the stage count is below.

### Load budget (estimate — parylene numbers `SOURCE NEEDED`)

Electrode at 2.25 mm pitch ≈ 2 × 2 mm = 4 mm². Parylene C, ε_r ≈ 3.15,
thickness 5 µm → `C = ε₀·ε_r·A/d ≈ 22 pF` per electrode. Energy per charge
`½CV² ≈ 0.70 µJ` at 250 V. Supply energy per charge+discharge cycle is
`CV² ≈ 1.4 µJ` (half stored, half lost in the HV507 output stage; the
stored half is lost on discharge). All 64 channels cycling at 1 kHz →
**≈ 89 mW ≈ 0.36 mA at 250 V.** Two corrections to the framing as given:

- "roughly 4 mA at 250 V worst case" is the **1 W supply budget**
  (4 mA × 250 V), not the load — the physics worst case is ~0.36 mA, an
  order of magnitude lower. Keep 1 W / 4 mA as the design capacity; keep
  0.36 mA as the load the droop math below is checked against.
- "realistic operation (a few electrodes at 10–100 Hz) well under 1 mW"
  holds for DC drive only. With **complement AC drive** (design-review
  item 1: every electrode AND the top plate flip each half-cycle at
  ~1 kHz) the all-64-toggle case is the *operating point*, so realistic ≈
  90 mW, not < 1 mW. Still sub-watt; the conclusion stands.

Add HV507 VPP quiescent current (`SOURCE NEEDED`: DS20005845A) and the top
plate's capacitance to the array through the ~100 µm gap (same order,
~45 pF total, only if the plate is switched — see Open questions). Parylene
inputs to source: ε_r (~3.1 at 1 kHz), dielectric strength (~220 V/µm →
5 µm at 250 V is 50 V/µm, ~4× margin), and the ≥ 7 µm-for-500 V figure
`pcb-ewod-multitile.md` already carries from `perplexity-research:338200`
(survey-derived, not a primary). The HV507 is a 300 V-class part, so 250 V
has margin, consistent with ruling 3.

### Topology: full-bridge-driven Cockcroft-Walton (Greinacher) ladder

- **Stage count from 20 V.** Half-bridge: the ladder sees 20 Vpp → N = 13
  for 250 V. Too many. **Full-bridge**: differential ±20 V → 40 Vpp →
  `Vout ≈ 40·N`: N = 6 → 240 V, N = 7 → 280 V; minus 2N Schottky drops
  (~0.4 V) → N = 6 ≈ 235 V, **N = 7 ≈ 274 V**. **Recommend N = 7,
  regulated down to 250 V** — built-in headroom. **N = 6 does not clear
  250 V; that is the trap.**
  *Reference subtlety the schematic must settle:* a full bridge's 40 Vpp
  exists between its two legs, not from one leg to board GND. A
  ground-referenced ladder fed from one leg sees only 20 Vpp (the N = 13
  case). To exploit 40 Vpp either (a) return the ladder's smoothing column
  to the second bridge leg and add a terminal diode + capacitor to board
  GND (a peak-hold; the ladder top then rides the second leg's 0–20 V
  square, and the hold captures its peak — `Vout ≈ 20·(2N+1)`: N = 6 →
  260 V, N = 7 → 300 V before drops), or (b) give the bridge a bipolar
  ±20 V supply via a one-stage inverting charge pump on the same clock,
  so one leg swings ±20 V about GND. Either way N = 7 lands at 274–294 V
  pre-regulation and N = 6 at 235–255 V — the recommendation is robust to
  the choice; the ripple/droop numbers below assume (a) or (b), not the
  one-leg case.
- **Why CW beats the flyback here.** *No magnetics at all* — deletes the
  single riskiest part on the board (the ~1:5 custom-ish flyback
  transformer, its leakage spike, the RCD clamp, and the bring-up that
  goes with them). Every ladder component sees ~2·Vpeak ≈ 40 V, not
  250–400 V: **100 V-rated** ceramics and 100 V Schottky/fast diodes. The
  400 V-output-cap DC-bias problem is *reduced, not eliminated*: X7R at
  40 V bias on a 100 V part still loses capacitance (`SOURCE NEEDED`: the
  chosen cap's DC-bias curve) — and 1 µF/100 V X7R is realistically
  0805/1206, not 0603; 470 nF/100 V fits 0805. Scales to 300 V by adding a
  stage.
- **Why the classic CW objection does not apply.** CW's bad name is its
  load-dependent droop, ∝ N³ (half-wave ladder; `SOURCE NEEDED`: a
  power-electronics reference for both formulas — import one):
  `Vdrop ≈ (I/(f·C))·(2N³/3 + N²/2 − N/6)`,
  `Vripple ≈ (I/(f·C))·N(N+1)/2`. At N = 7 the brackets are **252** and
  **28**. Against the 4 mA *budget*:

  | f | C | I/(fC) | Vdrop | Vripple | |
  |---|---|---|---|---|---|
  | 100 kHz | 1 µF | 0.040 V | 10 V | 1.1 V | acceptable |
  | 250 kHz | 470 nF | 0.034 V | 8.6 V | 1.0 V | acceptable |
  | 100 kHz | 100 nF | 0.40 V | **101 V** | 11 V | **the failure case — the easy mistake** |

  Against the physics load (0.36 mA) every figure is 11× smaller; at
  tens of µA (DC drive of a few pads) ~100× smaller — negligible. **The N³
  penalty that rules CW out elsewhere is irrelevant precisely because an
  EWOD array is a capacitive, sub-milliamp load.** Design floor: **f ≥
  100 kHz with C ≥ 1 µF, or 250 kHz with 470 nF**; 100 nF is the named
  trap. A full-wave (symmetrical, second driving column) ladder cuts droop
  ~4× and ripple ~2× for ~21 more parts (below) — cheap insurance,
  recommended, not required.
- **Part count, honestly.** Half-wave N = 7: 14 caps + 14 diodes = 28
  passives + bridge (4 FETs + driver) + control. Full-wave N = 7: 21 caps
  + 28 diodes ≈ 49 passives. The flyback is fewer parts (controller,
  switch, transformer, 400 V diode, 400 V cap, clamp) but one custom
  magnetic and 400 V-rated output components. **CW wins on height** — a
  ladder is all ≤ 1.5 mm parts, the transformer is ≥ 8 mm — which matters
  next to the top-plate hinge (Defect B).

### Build constraints (replace the flyback-specific ones)

1. **Full bridge: 4 FETs + gate driver from the 20 V rail, standalone
   oscillator** — the HV rail must not depend on firmware state; the Mini
   is never responsible for the 250 V existing. An enable line exists
   (default-on, ruling 13 sequencing).
2. **Regulation.** Correction to the framing: at sub-milliamp load a
   square-driven ladder settles at `N·Vpp` regardless of frequency or
   duty (droop is negligible — the same fact that made CW viable), so
   trimming f or D from the feedback divider has *no leverage*. Regulate
   by **hysteretic clock gating** (comparator on the divider gates the
   driver's enable; ripple = hysteresis band; the bleeder sets the
   discharge slope) — still standalone, no MCU — or by a **linear HV post-
   regulator** (series pass, dissipation ≤ (294 − 250 V) × 4 mA = 0.18 W
   worst case, ~16 mW at the physics load). Recommend clock gating; note
   the post-regulator as the low-ripple option.
3. **Feedback divider — KEEP verbatim:** a ~10 MΩ top leg burns 25 µA /
   ~6 mW at 250 V, acceptable, **but a single 0603 resistor is often rated
   only 50–75 V working voltage — the top leg is 3–4 resistors in
   series, never one.** The divider's bottom node (≤ 5 V) also feeds a
   Mini ADC pin (ruling 13 bring-up).
4. **Bleeder / minimum load — KEEP, duty restated.** The flyback's
   "runaway at no load" mode does not exist for a CW ladder, so the
   bleeder's job is purely the **safety discharge path** — and the stored
   charge is now distributed over the whole column (14 × 1 µF at ~40 V ≈
   11 mJ, comparable to a 1 µF/250 V output cap's 31 mJ). **The bleeder
   must drain the COLUMN, not just an output cap**: each stage capacitor
   discharges through the diode chain only if the output node is pulled
   down, so the bleeder sits across the output AND a discharge time to
   < 50 V is stated in the fab notes / runtime skill.
5. **Output filter:** a small 400 V-rated film or derated ceramic cap on
   the 250 V node for ripple; no large 400 V bank.
6. **The 250 V net class — IPC-2221B B4 0.4 mm** (rulings 3/10 landed the
   rows in `src/precis/data/pcb_capabilities.json`, read only by
   `capabilities.py::conductor_spacing_mm`). This now applies to the
   CONVERTER's output net and the ladder's stage nodes, not just the
   plaza. Findings at HEAD (2026-09-24):
   - `pcb-usb-c-pd-nano-testboard.md` §Blockers 3 ("`pcb_net_classes.rules`
     has no consumer") is **stale**: `rules.py::resolve_net_rules` is read
     by `drc.py::check_clearance` (`net_rules=`, per pair = max of the two
     nets' `clearance_mm`), by `realize.py` (`_resolve_track_rules`) and
     by `cost.py`. `op='class_rules'` is live.
   - **Blocker 1 (shipped 2026-10-15):** the maze grid keeps per-net
     clearance (`maze.OccupancyGrid(net_clearance_mm=)`, module docstring
     "Per-net clearance"); the grid base and pitch come from the smallest
     net clearance, the board-edge inset and plane pours from the widest.
   - **Blocker 2 — an authored class clearance is a WARN tier, not an
     ERROR tier:** `check_clearance` → `_two_tier(gap, jlc_min, required)`
     errors only below the fab floor (`jlc_min`); the class value only
     moves the WARN threshold (`generators.py` says so for the
     electrode-gap class: "changes the WARN threshold, never the ERROR
     one"). A 250 V creepage violation reported as a warning is not
     acceptable — Slice 4 adds a hard-floor key (`min_clearance_mm`, error
     tier) next to `clearance_mm`.
   - **Per-net working voltage does not exist.** Net classes are free-form
     `rules` dicts with known keys `clearance_mm`, `track_width_mm`,
     `layers`; `conductor_spacing_mm` is called only from
     `generators.py::resolve_ewod_sizing`. So the ladder's progressive
     potentials (40, 80, 120 … 280 V at N = 7) can be expressed only as
     explicit per-class `clearance_mm` values. B4 bands make this moot:
     ≤ 100 V → 0.13 mm, 101–300 V → 0.4 mm, so only the first two stage
     nodes could be tighter. **Decision: one `hv_250v` class with 0.4 mm
     for the whole ladder + output + VPP.** Slice 4 still adds a
     `working_voltage_v` class key resolving through `conductor_spacing_mm`
     so the number is derived, not typed (the ruling-3 discipline).
   - Also found: ruling 3 says B.Cu escapes take the B4 row, but the
     generator's `{name}_escape` class carries `clearance_mm = gap − slack`
     (~0.099 mm), not `hv_separation`; only plaza slot pitch and breakout
     geometry honour 0.4 mm today. Whether escapes *can* route at 0.4 mm
     clearance is exactly what Blocker 1's per-net clearance lets us
     measure — listed under Open questions, not decided here.

### Rejected alternative — flyback from the 20 V rail

> **Superseded 2026-10-16 by rulings 15/20/21:** a flyback is the chosen
> topology, now from the 5 V rail (D ≈ 0.71 CCM-ideal at 1:20, DCM in
> practice); the text below is the ladder-era rejection, kept as history.

Plain boost is out either way (D = 98 % from 5 V, 92 % from 20 V); a
flyback from 20 V needs ~1:5 turns (~1:20 from 5 V at D ≈ 0.7). Easier
regulation (a real current-mode loop) and fewer parts, **rejected** because
it needs a custom magnetic, an RCD clamp, a ≥ 400 V ultrafast diode and a
≥ 400 V output capacitor (DC-bias-derated ceramic or film) for a load that
justifies none of them.

## Rulings 16–19 — Reto, 2026-10-16 (morning-queue entry 5)

Ruled ~10:2xZ, relayed by the coordinator session witty-forging-bee-d4.

- **Ruling 16 — Slice 6 is a flyback.** Reto picked option (a): "rewrite
  slice 6 as a flyback". Slice 6 still described the Cockcroft-Walton
  ladder that ruling 15 had replaced. It is rewritten below.
- **Ruling 17 — parts.** Reto: "CH224K for the PD sink, CH340N for the USB
  bridge" *(the CH224K half is superseded by ruling 22)*. The buck converter and the flyback transformer were left to
  the builder, picked from JLC stock. The proposal is below. This lifts
  the "no C-numbers in this document" rule for those two parts only.
- **Ruling 18 — the top plate goes on a switched channel.** Reto picked
  option (b): "We want to invert plate and pixel to avoid droplet
  polarization (there's an invert pin)." The plate takes one HV507
  output instead of VPP. That sink's POL (the invert pin) sits on the
  same polarity net as every pixel sink's POL, so one POL edge flips the
  plate and all the pixels together. This settles former open question 1.
- **Ruling 19 — the escape nets carry `working_voltage_v = 250`.** Reto:
  "Yes". Measuring on the harness whether the fabric still routes at
  0.4 mm comes after the router reland. Do not run it before then. This
  settles the ruling half of former open question 2.

**Ruling 18, generator side — SHIPPED 2026-10-16** (worktree
`ewod-hv-s6`, generator version 9). The new key is
`sink_grid.top_plate_channel: "<channel pin>"`. It wires that pin on
sink 0 to `top_plate_net`, and no sink assigns it to an electrode. It
requires `pol_pin`, so every sink's POL rides the shared `{name}_pol`
net, and it is refused alongside `top_plate_pin`. The ledger records the
choice under `sink_grid._top_plate_channel`. The dogfood board still
ties the plate to VPP and switches when Slice 5 re-authors it.

**Ruling 19, generator side — BLOCKED on per-(net, layer) clearance.**
Ruled (Reto, 2026-10-16 ~16:05Z, via the fleet coordinator: "add the constraint when it is supported.") Apply it as soon as pcb-vector-router Slice 1
(per-(net, layer) clearance) lands; no workaround before then.
Putting `working_voltage_v: 250` on the `ewod_{name}_escape` class sets
a 0.4 mm ERROR floor on every layer. The array's own F.Cu plaza stubs
then sit closer than that to neighbouring electrode pads (for example
`track[ARR1_R0C2] <-> pad[ARR1/R1C2]` on F.Cu). `_pcb_apply`'s invalid-
board gate then refuses every put that declares a drive voltage: 19 test
failures on 2026-10-16, across dogfood, the generator and the island
terminal. The parylene same-footprint WARN does not cover them, because
the stub is fixed copper, not a pad. A per-net `working_voltage_v` is
not a substitute: it adds only the WARN-tier pairwise term, and two
escape nets both at 250 V read as a 0 V difference. The fix is the
per-(net, layer) clearance the scope list below excludes: 0.4 mm on
B.Cu, exempt for the F.Cu run under the dielectric. That is engine work
in `realize.py`, `drc.py` and `rules.py`. **Requirement handed to the
router redesign spec** (coordinator, 2026-10-16; step 1 of that spec is
one exact geometry checker shared by DRC and routing): on a parylene-coated
array, an escape net with `working_voltage_v: 250` gets a 0.4 mm ERROR
floor on B.Cu, and its F.Cu run under the dielectric is exempt. Nobody
builds it this wave.

### Part proposal (ruling 17) — buck and flyback transformer

Stock was read on 2026-10-16 at ~10:20Z, through the precis part search
and the LCSC product pages. Re-check it at footprint intake.

| Role | Part | LCSC | Stock | JLC tier | Why |
|---|---|---|---|---|---|
| ~~Buck 20 → 5 V~~ **deleted, ruling 22** | Diodes **AP63205WU-7** | **C2071056** | 43,955 | Extended | Fixed 5 V output, so no divider. Vin 3.8–32 V (abs max 35 V; 40 V for 400 ms), 2 A, 1.1 MHz. Datasheet: "the regulator enters low dropout mode by holding Q1 on for multiple clock cycles… The effective duty cycle is approximately 100%". It therefore passes vSafe5V through before the PD contract (ruling 13 sequencing). Package TSOT-23-6, plus L, bootstrap cap, input cap and output cap. |
| ~~Buck fallback~~ **deleted, ruling 22** | TI LMR51420XDDCR | C5246146 | 8,385 | Extended | Vin 4.5–36 V, 2 A, 98 % max duty; frequency drops in dropout. Needs a feedback divider. Its 4.5 V minimum Vin is tight against vSafe5V's 4.75 V floor. |
| Flyback transformer | Coilcraft **LPR6235-253PMRC** | **C19191684** | 2,914 | Extended | Coupled inductor sold for flyback and step-up use (Coilcraft Doc 752NR-1). **1:20, Lp 25 µH ±20 %, Isat 0.7 A, DCR 0.20 Ω / 72 Ω, SRF 580 kHz**, 6 × 6 × 3.5 mm SMD. At 20 V in, 250 V out needs D ≈ 0.38 (`Vout = n·Vin·D/(1−D)`). The switch sees ≈ 20 + 250/20 = 32.5 V plus the leakage spike, so a 60 V FET suffices. At 0.6 A peak and 200 kHz in DCM it delivers ≈ 0.9 W, which covers the 1 W budget; the operating point is ~90 mW. |
| Transformer fallback | Coilcraft LPR6235-253LML (1:10, 25 µH, Isat 1.3 A, same datasheet) | not found in JLC stock | — | — | D ≈ 0.56 at 20 V. More current headroom than the 1:20, but more switch stress. |

> **20 V figures in this table superseded 2026-10-16 by ruling 21.** The
> transformer row's "At 20 V in … D ≈ 0.38", "switch sees ≈ 32.5 V" and
> "0.6 A peak … ≈ 0.9 W" are for a 20 V input. At VIN = 5 V the guaranteed
> output is 0.45–0.65 W (open risk in ruling 21), and the switch sees
> ≈ 5 V plus the clamped leakage spike (clamp drain to VIN). The fallback
> row (1:10, D ≈ 0.56 at 20 V) is likewise a 20 V figure. The buck rows are
> deleted by ruling 22 (no 20 V contract, so no buck): the Mini, the HV507,
> the CH340N and the HV stage take the 5 V rail straight from VBUS through
> the ruling 22 PTC and optional load switch.

The ratio is 1:20, not the ~1:5 that ruling 15 sketched. 1:5 is the
minimum ratio that reaches 250 V. A higher ratio lowers duty and switch
voltage, and secondary current at sub-mA is not a constraint.

Rejected: C5382958 (LPR6235-123QMRC). Its 1:50 ratio is too high, its
secondary DCR is 200 Ω, and only 181 are in stock. No 1:5–1:12
photoflash-type transformer was found in JLC stock.

**Resolved at intake (Coilcraft Doc 752R, `perplexity-reasoning:492679`):**
- `PMR` is matte tin over nickel over silver; `PML` is the older
  silver-palladium-platinum glass-frit termination. Electrical values
  are the same (25 µH, 1:20). The final `C` is the 7" reel, 350 pcs.
- Winding-to-winding insulation is **300 Vrms for one minute**. That is
  functional insulation, not a safety rating. With ~250 V DC (≈ 354 V
  peak for an AC reading) across the windings the margin is about 1.6x
  on the Vrms figure, which is why the output ceiling and the HV clamp
  are required (ruling 20).
- Still open: both parts pay JLC's extended-part loading fee (no
  Basic-tier part exists for either role).

The **flyback controller** is not covered by ruling 17. The UC384x class
is dropped (ruling 20): UVLO start 8.4 V / stop 7.6 V, so it cannot
start from 5 V (`perplexity-reasoning:492683`). The controller pick is
LM3478MAX, see ruling 21.

### Ruling 20 — flyback build, reopened by Reto 2026-10-16

Reto chose to build the flyback on the board at ~16:25Z 2026-10-16: "Yes
build it now" (via the fleet coordinator). At ~16:3xZ he reopened the
component choices, via the fleet coordinator: "pick the FET; maybe a
proper DAC for a gentle ramp; skip the full module; is TPS61170+multiplier
really simpler?" A decision table with C-numbers comes from the
coordinator's decision agent for Reto to approve. Nothing below marked
held is decided.

**Settled:**
- Transformer: LPR6235-253PMRC (C19191684), facts above.
- UC384x fallback dropped (cannot start from 5 V).
- The XP Power A025P-5 bring-up module is dropped ("skip the full
  module"). Off-the-shelf HV modules run $100-300 and none is stocked by
  LCSC (`perplexity-research:492685`).
- Switching frequency stays under the transformer's 580 kHz SRF.
- ~~Input is the 20 V PD rail per rulings 15/17 (D ≈ 0.38). 5 V is also
  feasible (D ≈ 0.71).~~ **Superseded 2026-10-16 by ruling 21: VIN = 5 V.**

The ~~"Reto decides (pending decision table)"~~ list that stood here (controller,
topology, FET, diode and output cap, output ceiling and clamp, voltage set,
frequency) is **decided by ruling 21 below**.

### Ruling 21 — EWOD HV decision table, APPROVED by Reto ~18:10Z 2026-10-16

Approved by Reto (via the coordinator witty-forging-bee-d4, which holds his
EWOD-picks delegation: "You pick my decisions, and I approve"), ~18:10Z
2026-10-16. It replaces every "Reto decides" hold of ruling 20 and
**amends rulings 13 and 15**: the HV runs from the **5 V buck rail, not 20 V
VBUS**, and is **off by default** — the Mini enables it and kills it through
the controller's FA/SD pin.

| Role | Decision | LCSC |
|---|---|---|
| Topology | Flyback, DCM, ~200 kHz, VIN = 5 V | — |
| Controller | TI **LM3478MAX**, internal 4 ms soft-start, no SS pin. VSSOP alternate | **C115907** (alt. C404045) |
| Transformer | Coilcraft LPR6235-253PMRC (ruling 17) | C19191684 |
| Switch FET | **HL2310A**, Basic tier, 60 V, 125 mΩ at 4.5 V. Alternate DMN10H220L-7 | **C7420347** (alt. C102615) |
| Current sense | 0.27 Ω 0805, plus 100 Ω / 1 nF filter on ISEN | C47782 |
| Primary clamp | BZT52C24 zener plus B5819W Schottky, drain to VIN | C19077416 + C7420330 |
| Output diode | US1MF, 1 kV | C64877 |
| Output caps | 3 × 100 nF / 500 V 1206, kept off board edges and V-scores | C22374889 |
| HV clamp | 3 × 1SMA4763A 91 V in series (259–287 V) | C22393402 |
| Divider and bleeder | Top 5 × 1 MΩ, bottom 33.2 kΩ, bleeder 4 × 1 MΩ, all 1206 | C17927 |
| Voltage set | MCP4725A0T DAC through 2 × 49.9 kΩ, 2.2 µF from the midpoint to FB. DNP Mini-PWM resistor into the RC node as fallback | C144198 |
| Enable | Mini GPIO → 1N4148WS → FA/SD, 10 kΩ pull-up (off by default), DNP pull-down site | C2128 |
| Fallback (documented, not built) | TPS61170 plus an 8-stage Cockcroft-Walton ladder | — |

**Voltage-set polarity.** The DAC drives FB through the divider, so it can
only lower Vout from the hardware ceiling: ~255 V at DAC 0, and code 4095 ≈
0 V. The ceiling comes from the divider, independent of firmware.

**Ramp and kill sequence (firmware contract).** Power-up: General Call
Reset (DAC to its power-on state), write 4095, release FA/SD, ramp 8 LSB/ms
(≈ 0.5 V/ms), ADC check against the divider. Kill: FA/SD high plus code
4095.

**Datasheet facts kept from ruling 20** (TI LM3478): Dmax is 100 %; gate
drive equals VIN below 7.2 V; VIN 2.97–40 V; current-mode, so duty above
50 % needs slope compensation (Slice 6 sizes it at bring-up).

**OPEN RISKS — recorded, not decided:**
1. **Power.** 0.45–0.65 W is guaranteed from a 5 V input, not 1 W. The
   operating point is ~90 mW, so the budget has margin; the 1 W figure in
   earlier rulings is withdrawn.
2. **Hipot.** The transformer's 300 Vrms/1 min winding insulation is a
   test rating, not a working rating. Ask Coilcraft for the working
   voltage at ~250 V DC across the windings.
3. **MLCC DC bias.** Capacitance of the 500 V 1206 caps at 250 V DC is
   unknown; measure at bring-up and add a cap if the ripple needs it.
4. **Minimum load.** The bleeder value (4 × 1 MΩ) is tuned at bring-up
   against the DCM no-load behaviour.
5. ~~**20 V PD contract.**~~ Decided by ruling 22 below: dropped.
6. **Power budget on USB 5 V** (added by ruling 22): the whole board must
   fit about 1.3 W (roughly 300 mA at 5 V). Re-check against the 0.45–0.65 W
   guaranteed HV output in risk 1 at bring-up.

### Ruling 22 — drop the 20 V USB-PD contract; 5 V Type-C sink (Reto, ~20:10Z 2026-10-16)

Reto approved it as proposed, ~20:10Z 2026-10-16, via the coordinator
witty-forging-bee-d4. It decides Open question 3 and **supersedes** every
20 V passage above and below: rulings 13 (PD negotiation, CH224K, bring-up
sequencing), 15 (20 V rail), 17 (buck, CH224K picks), the Pro Mini RAW-pin
20 V trap, and Slice 5's 20 V sequencing. Rulings 12, 14 and 16–21 stand
otherwise. The 5 V rail is unchanged for the Mini, the HV507 VDD, the
CH340N and the HV stage (ruling 21).

- **Deleted:** the 20 V PD contract, the CH224K and its CFG straps, the PD
  VBUS divider and PD-failed LED, and the AP63205 buck (C2071056) with its
  inductor, bootstrap and caps. The LMR51420 buck fallback goes too.
- **CC1 and CC2** each get a 5.1 kΩ pull-down to GND. The board is a
  default-5 V Type-C sink and draws up to 900 mA (USB 2.0 rule) from any
  source; the budget below stays well under that.
- **VBUS entry:** VBUS → PTC resettable fuse → optional soft-start load
  switch → the 5 V rail, with a TVS from VBUS to GND ahead of the fuse. The
  load switch limits the inrush at plug-in so the rail does not spike. It is
  optional: leave it DNP and bridge it with a 0 Ω jumper if bring-up shows
  the PTC and bulk cap are enough.
- **Budget:** about 1.3 W for the HV stage plus logic, roughly 300 mA at
  5 V.
- **VBUS and RAW:** VBUS is 5 V now, so the 20 V RAW-pin trap is gone. RAW is
  still left unconnected; the rail feeds the Mini's VCC pin.
- **Footprints:** the 11 footprints cached for ruling 21 are unchanged.

**VBUS-entry part picks** (stock read from the precis part search,
2026-10-16; re-check at footprint intake). All three are JLC
Extended. **Datasheet status: NOT attached.** The prod `datasheet` kind in
this build refuses `put` (`[error:Unsupported] datasheet does not support
put`), and the only other route (a `datasheet_pull` job) needs a part ref
minted by a link or a pcb edit, which this brief did not grant. Per the
datasheet-first rule these are **candidates until a pull lands**; the
URLs below are the catalog's `datasheet_url` for each part.

| Role | Part | LCSC | Stock | Key ratings | Datasheet |
|---|---|---|---|---|---|
| PTC fuse | BHFUSE **BSMD1206-050-6V** | **C883122** | 47,907 | 1206, hold 0.5 A, trip 1 A, 6 V max, 100 A max fault, 150 mΩ min / 700 mΩ max | [LCSC PDF](https://www.lcsc.com/datasheet/lcsc_datasheet_2305121423_BHFUSE-BSMD1206-050-6V_C883122.pdf); `put(kind='datasheet')` pull failed 2026-10-17 (`not_pdf`: LCSC serves HTML to the worker), operator drop needed |
| VBUS TVS | MDD **SMF5.0A** | **C193402** | 557,643 | SOD-123FL, unidirectional, 5 V standoff, 6.4 V min breakdown, 9.2 V clamp at 21.7 A, 200 W (10/1000 µs), 400 µA leakage | [LCSC PDF](https://www.lcsc.com/datasheet/lcsc_datasheet_2308231016_MDD-Microdiode-Semiconductor-SMF5-0A_C193402.pdf); `put(kind='datasheet')` pull failed 2026-10-17 (`not_pdf`: LCSC serves HTML to the worker), operator drop needed |
| Load switch (optional) | TI **TPS22918DBVR** | **C131941** | 60,734 | SOT-23-6, 1–5.5 V, 2 A, 53 mΩ, active-high, CT pin sets the turn-on slew | pulled from `ti.com/lit/ds/symlink/tps22918.pdf` (the LCSC URL returned HTML): `get(kind='datasheet', id='instruments26')` |
| PTC alternate | nSMD050 | C69688 | 22,290 | 1206, hold 0.5 A, 6 V | pull failed 2026-10-17 (`not_pdf`, LCSC HTML), operator drop needed |
| TVS alternate | MDD-class SMF5.0A | C169426 | 51,916 | SOD-123FL, same family footprint | pull failed 2026-10-17 (`not_pdf`, LCSC HTML), operator drop needed |

The fuse hold current (0.5 A) sits above the ~300 mA budget and below the
USB 2.0 900 mA limit; the 6 V rating clears VBUS at 5.25 V. The TPS22918's
turn-on time at its CT pin is set at bring-up; its 2 A rating covers the
fuse trip current.

**Sources.** TI `lm3478.pdf` and `tps61170.pdf`; Microchip `22039d.pdf`
(MCP4725); Coilcraft LPR6235 datasheet
(https://www.coilcraft.com/getmedia/1aa7eee3-2e1c-41c5-a485-234fb57f5458/lpr6235.pdf);
rationale in `perplexity-reasoning:492700`.

## Defect A — a generated package does not declare all its pins

Reto: "ARR1_sink_0_0 does not have all the pins, a package should always
have all the pins."

**Root cause** (`generators.py::_expand_ewod_pad_array`, sink emission): the
sink component's `pin_decls` is **wire-driven** — it appends the channel
pins in this sink's own `share`, then `serial_in_pin`, `serial_out_pin`,
the optional `top_plate_pin`, and the `power` map's keys. Every other
HV507 pin (CLK, LE, BL, POL, DIR, C, HVGND, every unassigned HVOUTn) is
never declared. Ruling 6's own text admits the symptom ("an unwired spare
pin has no IR pin id to swap and is never listed"); with ruling 2's
`channels_per_sink` balancing a partly-filled sink declares fewer than 64
channel pins. `_SinkGrid` is deliberately part-agnostic (`expand()` is
pure, no DB), so it *cannot* know the footprint's pin names.

**Why it matters** — same blindness class as gr339236 and gr346744:
`realize.py::pads_for_ir` is "every placed **pin** as a pad", so a pad
with no declared pin is invisible to `check_clearance`, to connectivity,
and to the router's occupancy stamping (`_stamp_pads`). Only the gerber/
SVG path (`padplace.board_pads`, which iterates the footprint's own pad
list) draws all 80 pads — so fab output and DRC describe different boards,
the exact drift round 8 removed for positions.

**Fix direction — declare EVERY pad the footprint names; unwired ones are
NC.** The join already exists: `session.py::_real_pin_offsets` keys pads by
`pin_map[str(pad.number)].name`, falling back to the pad number when the
`pin_map` has no entry. The generator stays pure; the completion belongs
**in the store layer, kind-wide**: `_pcb_ops.py::_pcb_apply`, when the
component's footprint is cached (`part_footprints` by C-number or
`pcb_local_footprints` by name), declares a pin for every `pin_map` name
(and every unnamed pad, by number) the component did not declare, with no
connection. `ir.py::from_graph` already turns a connection-less pin into
`NO_NET`, and `realize.py::_stamp_pads` already gives each NO_NET pin its
own sentinel owner (`ir.n_nets + pid`) so NC pads are routing obstacles
distinct from each other. Because the footprint may not be cached at apply
time (`op='footprint'` runs later), a DRC rule `undeclared_pad` (footprint
pad with no declared pin; fires only when the footprint IS cached — "a
check that cannot fire must say so") is the safety net, and a re-apply
completes the declarations.

**Effects checked:**
- **Pin swapping (ruling 6): none.** The generator's admissible set is
  `list(channel_map)` — wired pins only — and
  `pcb_route._resolve_pin_swap_groups` drops any member whose rotation-CSR
  degree differs from the group's (an NC pin has degree 0). Declaring NC
  pins changes nothing unless they are *added* to the set; making spare
  channels swap targets would need `PinSwapGroup` to move a net onto a
  netless pin — a follow-on, not this item.
- **Placement clearance: courtyards grow to the real package.**
  `ir.py::instance_courtyard_polygon` is the hull of the instance's own
  *pad outlines*, so today a 40-pin-declared sink has a courtyard smaller
  than its 80-pad body; after the fix `check_courtyard_overlap` and the
  placer see the whole package. Expect the dogfood's hand-fixed placements
  to need a look.
- **DRC same-net exemption:** `pads_for_ir` labels an NC pad `net: ""`, so
  two NC pads are same-net to `clearance_pairs_indexed` and not checked
  against each other (fab-defined pitch inside one package — harmless);
  NC-vs-real-net pairs are checked, which is the point.
- **HV507 specifics** (memory: 77 named of 80 pads, 12 distinct non-channel
  names → at least one name is on two pads). `_real_pin_offsets` is
  first-pad-wins per NAME, so the second same-named pad stays invisible
  even after completion (the multi-pad-per-pin story of gr339236); the 3
  unnamed pads become pins named by number. Record both in the
  `undeclared_pad` finding text rather than silently accept.

## Defect B — no copper-tape landing for the ITO top plate

Reto: "we lack the copper tape landing side for the ITO." This is **ruling
4 (2026-09-18), never built** — no `tape_land` string exists in `src/`.

**What exists vs what is unbuilt** (HEAD, 2026-09-24):

| Piece | State |
|---|---|
| per-pad `mask: open\|covered`, `paste: none\|full` | BUILT — `_pcb_ops.py::_LOCAL_PAD_MASKS`, `gerber.py::soldermask_gerber`/`solderpaste_gerber` |
| pad `role` vocabulary | BUILT as `("solderable","electrode","probe")` — `_pcb_ops.py::_LOCAL_PAD_ROLES`; **`tape_land` is not in it** |
| drill-free SMD pad on F.Cu | BUILT (a pad with no `drill`) |
| region-level `mask_open` feature | BUILT — `ftype: mask_open`, emitted by the generator, consumed as `model["mask_open_regions"]` |
| `pcb_fixed_copper` (migration 0165) | BUILT for `ctype in (track, via)` only — **no mask/paste semantics**, so the landing cannot be fixed copper; it is a PAD |
| `role: tape_land` strip emitted by the generator | **UNBUILT** |

**Build**: a `tape_land` param on `ewod_pad_array` (`edge: N|S|E|W`,
`length_mm`, `width_mm` default ~5 mm, `offset_mm` from the array edge)
emits ONE rect pad on the array's own local footprint with `role:
tape_land`, `mask: open`, `paste: none`, no drill, on F.Cu along the hinge
edge; the array component gains a pin `top_plate` connected to
`sink_grid.top_plate_net` (VPP on the dogfood config) — the same net the
sink's `top_plate_pin` is on, so the ordinary router closes F.Cu → via →
B.Cu (no layer lock on this net). Ledger + capability map show the strip.
Fab notes gain: "mask the tape land during parylene deposition" (parylene
is conformal; a coated land is an open circuit) next to the existing
process-order note. `role: tape_land` joins the allowlist with the
`probe`-like defaults (paste none). Silk stays off it.

## In scope — slices (dependency order; each independently shippable)

**Slice 1 — every footprint pad is a declared pin (Defect A).**
**SHIPPED 2026-10-13 under `pcb-lazy-netlist-and-checks.md` Slice 2**
(commit `fix(pcb): declare every footprint pad as a pin with intent
markers; close slice 3`, worktree `bl-pcb-netlist-s23`): that slice took
Defect A over, added the §1j intent markers and the phase-aware
`unconnected_pin`, and carried Defect A's nuances into its own §1j.
`_pcb_apply` completes pins from the cached footprint (`pins_completed`
in the put response); `undeclared_pad` is the safety net; the generator
declares every channel pin (`unused` on the unwired share) at version 5.
Nothing remains here — the Defect A analysis above stays as the diagnosis.

**Slice 2 — `sink_grid` control pins as named config. Pure generator
work.** **SHIPPED 2026-10-15** (worktree `ewod-board`): `clk_pin`,
`le_pin`, `bl_pin`, `pol_pin`, `dir_pin` are named config; each driven
line gets one `{name}_<ctl>` net bussed to every sink (ledger
`sink_grid._control_nets`); a pin named twice (control + power + serial +
channels) is a `ValueError`; `HVGND`/tied DIR/BL stay on the `power` map.
Generator version 8.

**Slice 3 — `role: tape_land` strip (Defect B / ruling 4). Pure generator +
store work.** **SHIPPED 2026-10-15** (worktree `ewod-board`): `tape_land`
param `{edge, length_mm, width_mm~5, offset_mm}` emits one drill-free
rect pad (`role: tape_land`, `mask: open`, `paste: none`, F.Cu) along the
configured array edge; the array gains pin `top_plate` wired to
`sink_grid.top_plate_net`. `tape_land` joined `_LOCAL_PAD_ROLES`
(paste-none default). New: the fab zip carries `<slug>-fab-notes.txt`
when a model has EWOD content — the parylene process-order note (built
here, the "existing" note never actually shipped) plus the tape-land
masking sentence. Ledger `tape_land` + capability map draw it.
*Open:* `connectivity`-after-`op='route'` acceptance is a routed-board
fact, deferred to Slice 7's harness run.

**Slice 4 — HV net class end-to-end. Engine work, no footprints.**
(a) per-net clearance in the grid — shipped (Blocker 1);
(b–d) **SHIPPED 2026-10-15** (worktree `ewod-board`): `NetRules` gains
`min_clearance_mm`; `resolve_net_rules` reads class `min_clearance_mm`
(explicit error floor) and `working_voltage_v` (derived through
`conductor_spacing_mm` — external=B4 coated per ruling 3, internal=B1),
both feed the floor AND raise `clearance_mm` so the router keeps the
spacing; `check_clearance` errors below the pair floor (message names
the class floor); `_pcb_upsert_net_classes` refuses `working_voltage_v`
past the 500 V band; `ewod_pad_array` emits `ewod_{name}_hv` with
`working_voltage_v` for the top-plate net when `drive_voltage_v` is
declared. Board authoring of ladder/output nets rides `op='class_rules'`.
*Accept:* a two-net fixture with one 0.4 mm class and one default net
routes the default net at fab clearance and keeps 0.4 mm around the HV net;
an HV-vs-GND gap of 0.3 mm is a DRC **error**, 0.45 mm clean; a class with
`working_voltage_v: 250` resolves to 0.4 mm and `working_voltage_v: 600`
is refused (table top band); existing electrode-gap class behaviour
(WARN threshold only) unchanged.

**Slice 5 — controller + USB-C 5 V power on `ewod-dogfood-6` (ruling 22).
BLOCKED on footprint intake.** Pro Mini LOCAL footprint (verified drawing);
`op='footprint'` intake for: USB-C 16-pin receptacle, the VBUS PTC fuse,
VBUS TVS and optional load switch (ruling 22 table), CH340N (ruling 17),
ESD array, LEDs. Footprints cached 2026-10-17 (part_footprints 13 → 24): USB-C
C2765186, CH340N C2977777, ESD array C7519 (USBLC6-2SC6), PTC C883122, TVS
C193402, load switch C131941, transformer C19191684, 0603 5.1 kΩ C23186,
10 kΩ C25804, 100 nF C14663, LED C12624. **Ruled (Reto, 2026-10-17
~09:50Z, via the fleet coordinator: "As recommended, yes"):** the 0603
passives and the LED (5.1 kΩ C23186, 10 kΩ C25804, 100 nF C14663, LED
C12624), the CH340N C2977777 and the ESD array C7519 are accepted.
Alternates C69688/C169426 not cached.
**Pin status (datasheet-first):** CH340N C2977777 footprint pins checked
against the WCH CH340DS1 datasheet (stored as `datasheet:roberto25`; its
title metadata is wrong, the chunks are the CH340 sheet): 1 UD+, 2 UD-,
3 GND, 4 RTS#, 5 VCC, 6 TXD, 7 RXD, 8 V3, all eight match the SOP-8
column. USB-C C2765186 is **pins unverified**: no datasheet stored (the
Shouhan PDF is an operator drop). Its footprint pins (A1B12/B1A12 GND,
A4B9/B4A9 VBUS, A5 CC1, B5 CC2, A6/B6 Dp, A7/B7 Dn, A8/B8 SBU, 13/14 EH)
follow the generic Type-C pinout only. **Operator PDF drop for Reto**
(LCSC returns HTML; no manufacturer URL reachable): BHFUSE BSMD1206-050-6V
(C883122), TECHFUSE nSMD050 (C69688), MDD SMF5.0A (C193402; Verical copy
returned 403), Jingdao SMF5.0A (C169426), Shouhan TYPE-C 16PIN 2MD(073)
(C2765186). There is no PD controller and no buck (ruling 22). Netlist per
rulings 12/22: CC1 and CC2 each carry a 5.1 kΩ pull-down to GND; VBUS
reaches the 5 V rail only through the PTC (then the optional load switch),
with the TVS from VBUS to GND; the bridge's VCC is on the 5 V rail. The
Mini's RAW stays unconnected by convention (the rail feeds its VCC pin
directly), and the netlist test keeps asserting it.
*Accept:* `view='footprints'` shows no synthesized pin on any new part;
netlist assertions above; board places and routes on the offline harness
with the fabric count no worse than the HEAD re-measure (prerequisite).

**Slice 6 — flyback HV stage on `ewod-dogfood-6` (rulings 15/16/21).
BLOCKED on footprint intake** (Slice 4 has shipped). Every part is now
decided by ruling 21 (APPROVED 2026-10-16): LM3478MAX controller, HL2310A
switch, 0.27 Ω sense with the 100 Ω / 1 nF ISEN filter, the BZT52C24 +
B5819W primary clamp (drain to VIN), LPR6235-253PMRC transformer, US1MF
output diode, 3 × 100 nF / 500 V output caps, the 3 × 1SMA4763A HV clamp,
the 5 × 1 MΩ / 33.2 kΩ divider with a 4 × 1 MΩ bleeder, the MCP4725A0T
voltage-set DAC and the FA/SD enable gate. C-numbers are in the ruling 21
table. VIN is the **5 V rail** (VBUS through the ruling 22 PTC and
optional load switch), not a 20 V rail. The stage is **off by
default**: FA/SD is pulled up by 10 kΩ and the Mini releases it (ruling
21), so ruling 15's "standalone" and ruling 13's "default-on enable" are
superseded. The divider's top leg is 5 series resistors, and its bottom
node (≤ 5 V) feeds both the controller's FB pin and a Mini ADC pin. The
bleeder doubles as the safety discharge path and the DCM minimum load
(tuned at bring-up). The 250 V net is VPP. It feeds HV507 VPP and, after
ruling 18, reaches the plate only through a switched channel. Output caps
sit off board edges and V-scores.

*Generator work not done here.* The footprints (LM3478 SOIC-8/VSSOP-8, SOT-23
FET, 0805/1206 passives, SMA zener, SOD-123F, LPR6235, MCP4725 SOT-23-6)
need `op='footprint'` intake on prod; that is a prod pcb edit and is out of
scope for the docs-only table landing. Next slice: footprint intake, then
the `ewod_hv_stage` authoring with the netlist assertions below.

*Accept:* the 250 V output/VPP net and the transformer secondary are in
the 0.4 mm class (`working_voltage_v: 250`) and DRC-clean at ERROR tier;
the divider top leg is ≥ 3 series resistors (netlist assertion; the
design uses 5); a bleeder connects the 250 V node to GND (netlist
assertion); the transformer primary side connects to the 5 V rail and no
VBUS net touches the HV stage (netlist assertion); FA/SD has a pull-up to
5 V (netlist assertion, off by default); fab notes state the discharge time
to < 50 V.

**Slice 7 — prod acceptance after deploy.** Re-apply `ewod-dogfood-6`,
`op='route'`, `view='drc'`, `view='gerber'`; the "Read first" note
retires; J_INSTR re-checked.
*Accept:* `unrouted` on prod equals the offline harness within the
seed-margin convention; `synthesized_footprint 0`; no F.Cu track ends over
a B.Cu pad without a via (the gr346744 signature) — else file it as new.

## Explicitly NOT in scope

- Firmware (frame timing, complement drive) — the
  kind ships copper and metadata.
- Multitile controller scaling (Teensy return), capacitive sensing path,
  DropBot-style feedback — recorded as the known ceiling only.
- Spare-channel pin swapping onto netless pins (follow-on named in
  Defect A).
- Per-(net, layer) clearance (an escape net that is exempt on F.Cu under
  the dielectric but 0.4 mm on B.Cu) — Open question, not built here.
- Rewriting `pcb-usb-c-pd-nano-testboard.md` beyond marking its blocker 3
  stale.
- Choosing C-numbers in this document, except the flyback transformer
  proposal that ruling 17 asks for, the HV decision table of ruling 21
  (approved by Reto) and the VBUS-entry picks of ruling 22.

## Acceptance criteria (summary — detail per slice above)

1. Every placed footprint pad is an IR pin; DRC/router/connectivity and
   gerber describe one board (Slice 1).
2. HV507 control pins are config, with externally-facing nets (Slice 2).
3. A mask-open, paste-free, drill-free `tape_land` strip on the top-plate
   net is emitted, exported and connected (Slice 3).
4. A 0.4 mm class binds the router per-net and DRC at error tier, derived
   from `working_voltage_v` (Slice 4).
5. `ewod-dogfood-6` carries Pro Mini + USB-C 5 V sink (ruling 22) + bridge
   + flyback HV stage from the 5 V rail with the netlist traps asserted
   (Slices 5–6).
6. Prod matches the offline harness after deploy (Slice 7).

## Target + blast radius

- Store: `_pcb_ops.py` (`_pcb_apply` pin completion, `_LOCAL_PAD_ROLES`,
  class-rule keys) — no migration expected (pins/roles/rules are existing
  columns and jsonb).
- Engine: `drc.py` (`undeclared_pad`, error-tier class floor),
  `rules.py::resolve_net_rules` (`min_clearance_mm`, `working_voltage_v`),
  `realize.py`'s per-net clearance resolution (the `_realize_vector` +
  `vroute.Space` pair named here was deleted with the router on
  2026-10-25 — the same rules resolve via `topo.build_region_graph`
  now), `generators.py`
  (`_SinkGrid`, `tape_land`, HV class), `capabilities.py` (reader reuse).
- Handlers: `handlers/pcb.py` put schema (`sink_grid` keys, `tape_land`,
  class-rule keys).
- Skills: `precis-pcb-ewod-help`, `precis-pcb-help` (class keys —
  `precis-pcb-route-help` was deleted with the router).
- Tests: `test_pcb_ewod_generator*.py`, `test_pcb_ewod_dogfood.py`,
  `test_pcb_island_terminal_polygon.py`, reference-fixture pins.

## Sources — cited vs SOURCE NEEDED

Cited from existing specs: `perplexity-research:338200` (parylene
thickness/voltage, survey-grade); IPC-2221B B1/B2/B4 rows already in
`pcb_capabilities.json` (rulings 3/10, verified against the table);
`pcb-usb-c-pd-nano-testboard.md` (PDO set, sink-controller survey).

`SOURCE NEEDED` (import, then cite; no ids invented here): Microchip
HV507 datasheet DS20005845A (V_IH, VDD, VPP quiescent, pin functions incl.
`C`, DIR); SparkFun Pro Mini 5 V/16 MHz schematic + drawing (pinout, RAW
LDO limit); WCH CH224K datasheet (CFG selection, behaviour when the
requested PDO is absent; moot after ruling 22, which drops the CH224K);
ATmega328P safe-operating area (16 MHz vs VCC); parylene C ε_r and
dielectric strength (a primary, not the survey); a power-electronics
reference for the CW droop/ripple formulas; the chosen ladder capacitor's
DC-bias curve; **EWOD precedent** — whether OpenDrop (Alistar & Gaudenz
2017) and DropBot use a CW multiplier or a flyback for their HV rail: not
asserted either way, a literature check directly relevant to this board
(`pcb-ewod-multitile.md` calls the HV507 "OpenDrop-proven" with no paper
id — import the OpenDrop paper and cite it there too).

## Open questions / Reto's calls

1. **Escape nets at B4 clearance: does the fabric still route?**
   Ruling 19 put `working_voltage_v: 250` on the escape class. Whether
   the fabric still routes at 2.25 mm pitch is measured on the harness
   after the router reland, not before. If F.Cu electrode adjacency must
   stay exempt, this needs per-(net, layer) clearance.
2. ~~**Flyback parts and control** — held, ruling 20.~~ **Decided
   2026-10-16, ruling 21.** The residual risks (power, hipot, MLCC DC bias,
   bleeder tuning) are listed there.
3. ~~**Is the 20 V PD contract still needed?**~~ **Decided 2026-10-16,
   ruling 22: no.** The contract, the CH224K and the buck are dropped; the
   board is a default-5 V Type-C sink.

(Former questions settled: the top plate goes on a switched channel
(ruling 18), and the full-wave versus half-wave ladder question went
with the ladder (rulings 15/16).)

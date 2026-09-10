---
status: draft
title: "pcb pre-tapeout checklist — curated seed items (companion to checklist-kind.md)"
prio: high
---

# pcb pre-tapeout checklist — curated seed items

Source: cached `perplexity-research` report, query "Comprehensive PCB design
review checklist of industry best practices, organized by phase..." —
retrievable via `get(kind='perplexity-research', q='<that query>')` or
`search(kind='perplexity-research', q='<keywords>')`. Curation rule: keep
what names a real, concrete failure; discard cargo cult (see Discarded
below). Context assumed: small-run boards fabbed/assembled at JLCPCB,
designed by an LLM-guided place+route system with an existing geometric
DRC (clearance, trace width, annular ring, courtyard, silk, board edge,
connectivity, unrouted) — so per-rule DRC restatements are never emitted
as standalone items; they collapse into the coarse tool items below.

## Tool items (bridges to the rules layer — never decomposed)

| item | phase | decidability | prevents |
|---|---|---|---|
| netlist-exceptions-clean | netlist | tool | Unconnected pins, single-node nets, or unresolved measure operands reach layout undetected. |
| drc-clean | layout | tool | Any of the 15 encoded geometric/connectivity rules (clearance, annular ring, courtyard, silk, etc.) is violated on the fabricated board. |
| all-nets-routed | layout | tool | An unrouted net ships to fab as an open circuit. |

## Schematic

| item | phase | decidability | prevents |
|---|---|---|---|
| connector-mates-counterpart | schematic | judgment | Gender/pinout mismatch against the real mating cable or header swaps power or reverses a diff pair — passes ERC, non-functional in hardware. |
| rail-readback-considered | schematic | judgment | No spare-ADC divider on a supply rail means a brownout or sag looks like an unrelated comms/reset fault with no way to confirm it in-system. |
| current-budget-per-rail | schematic | judgment | Per-rail load was never summed against the regulator's rated current, so the rail droops or the regulator trips overcurrent under real load. |
| regulator-dropout-margin | schematic | judgment | Upstream supply sag (battery discharge, cascaded regulator) pulls the rail out of regulation because dropout margin was never checked against worst-case input. |
| regulator-thermal-margin | schematic | judgment | Regulator or IC overheats or thermally shuts down because dissipated power vs. thermal resistance/copper area was never calculated. |
| decoupling-adequate | schematic | judgment | Missing, undersized, or wrong-ESR decoupling on a power pin causes regulator oscillation, ground bounce, or EMI susceptibility that surfaces as intermittent failures later. |
| pull-resistors-present | schematic | judgment | A bus (I2C, open-drain) sits stuck low, or a logic input floats, because a required pull-up/pull-down was omitted or undersized. |
| strap-boot-pins-vs-datasheet | schematic | judgment | A floated or mis-strapped configuration pin boots the device into the wrong mode (bootloader instead of application, wrong boot source). |
| reset-supervision | schematic | judgment | Missing reset filtering/pull-up or bad supervisor sequencing leaves the device in an indeterminate state at power-up or causes spurious resets from noise. |
| unused-pin-handling | schematic | judgment | A floating unused pin acts as a noise antenna, or a floating config input leaves the part in an undocumented mode. |
| esd-tvs-at-connectors | schematic | judgment | An ESD event at an exposed connector damages or latches up the downstream IC because TVS protection was missing or undersized for that interface. |
| level-shifting-domains | schematic | judgment | A voltage-domain crossing without a level shifter overvoltages an IO pin or is misread as a logic level, causing damage or comms failure. |
| test-points-present | schematic | judgment | No way to probe or reprogram the assembled board without bodge wires, because no test point was placed for a signal needed at bring-up. |
| mounting-hole-grounding-intent | schematic | judgment | A mounting hole's PTH/NPTH choice doesn't match the intended chassis-ground or isolation, creating a ground loop or losing an intended EMI-shield path. |
| power-on-sequencing-planned | schematic | judgment | Rails rising in an undefined order latch up mixed-voltage ICs or back-power a domain through IO protection diodes, damaging parts or hanging the boot. |
| inrush-protection-present | schematic | judgment | Bulk-capacitance inrush at plug-in droops the upstream supply into a brownout-reset loop or pits connector contacts; no soft-start/load-switch slew control was provided. |
| rail-switches-mcu-controlled | schematic | judgment | Without MCU-controlled high-side switches on subsystem rails, a hung peripheral cannot be power-cycled in-system and a faulty domain cannot be isolated during bring-up. |
| watchdog-provision | schematic | judgment | Hung firmware permanently wedges the board with no recovery path because no watchdog (external chip, or at minimum a plan for the MCU's internal one) can reset it. Applies: unattended/remote operation. |
| spare-pin-self-diagnostics | schematic | judgment | Leftover MCU inputs left unallocated instead of wired to diagnostic senses (presence detects, ID straps, temperature) leave field faults undiagnosable in-system — the digital counterpart of rail-readback-considered. |
| bringup-self-check-plan | schematic | judgment | No planned power-on self-test means first bring-up cannot distinguish a board fault from a firmware fault — and the senses the POST would need (rail dividers, ID pins) were never provisioned because no plan demanded them. |
| programming-access-provision | schematic | judgment | No flash/debug access point (Tag-Connect-style pin-free pad footprint, or a header) on a board with a factory-blank MCU means assembled units cannot be programmed without hand rework. |

## Netlist

| item | phase | decidability | prevents |
|---|---|---|---|
| functional-proximity-intents-declared | netlist | judgment | A sensor whose reading depends on physical placement (temp sensor at the hot part, light/pressure sensor with its stimulus, mic away from a noise source) gets placed by wire-length alone because no `proximity`/`separation` measure declared the intent — the board works electrically and measures the wrong thing. |

## Layout

| item | phase | decidability | prevents |
|---|---|---|---|
| footprint-vs-datasheet | layout | judgment | A footprint rotated, mirrored, or pin-mapped wrong on a symmetric-looking part solders fine and passes DRC but is completely non-functional. |
| pin1-polarity-silkscreen | layout | judgment | Missing or wrong pin-1/polarity silkscreen causes assembly (human or machine) to seat a polarized part backwards, destroying it at first power-on. |
| connector-mechanical-fit | layout | judgment | A connector or board edge doesn't clear the mating cable, enclosure wall, or neighboring part — discovered only at physical assembly, forcing a full respin. |
| board-shape-mountable | layout | judgment | The board outline or mounting-hole placement doesn't actually fit the intended enclosure or standoff pattern, found only after the board is in hand. |
| return-path-continuity | layout | judgment | A signal or analog return crosses a ground-plane split or gap, injecting a reference error (ADC inaccuracy) or coupling digital switching noise into an analog section. |
| crystal-clock-layout | layout | judgment | Vias on a clock line, excess loop area, or missing ground/cap placement around the crystal keeps the oscillator from starting or adds enough jitter to break timing-sensitive comms. |
| antenna-keepout | layout | judgment | Copper or a component inside the antenna keepout detunes it, cutting range or failing radio certification. Applies: boards with an RF antenna. |
| thermal-via-stitching-adequate | layout | judgment | A thermal pad has too few stitching vias for the power actually budgeted to it, so the part overheats despite passing the generic thermal-relief pattern check. |
| test-point-accessibility | layout | judgment | A test point ends up buried under a part or with no probe clearance, defeating the point of having placed it. |
| soft-measure-residuals-acceptable | layout | judgment | A `soft` measure the placer traded away (rubber band left stretched) ships unreviewed: the eyes report the violation-mm but nobody makes the call — "cap 3mm from the pin" may be fine while "temp sensor 40mm from the hot part" defeats the design and needs serious re-placement work, and without an explicit per-residual verdict the second ships as silently as the first. Evidence anchor: the measures view. |
| test-pads-jig-ready | layout | judgment | Test pads placed at arbitrary coordinates instead of on an even-coordinate grid (ideally grouped along one edge) make a bed-of-nails test jig impractical, so every unit must be hand-probed forever. A test pad is just a component: widened exposed copper, one pad, silk label — free to place on-grid now, expensive to retrofit. |

## Fab

| item | phase | decidability | prevents |
|---|---|---|---|
| stackup-vs-fab-capability | fab | judgment | The stackup JLC actually builds (dielectric heights, copper weights) diverges from the one assumed for a controlled-impedance or diff-pair calculation, silently missing the target impedance. |
| bom-availability-lifecycle | fab | judgment (TTL) | A BOM line is NRND/obsolete or references the wrong voltage/variant suffix on an electrically-similar footprint, stalling the order or substituting a part that doesn't work as designed. |
| gerber-export-integrity | fab | judgment | A missing layer, swapped polarity, or netlist-vs-copper mismatch in the exported Gerbers ships to fab undetected — must render red, not absent, until the export-side check is wired (`pcb-fab-output-unwired.md`). |
| drill-npth-grounding-intent | fab | judgment | A hole built as NPTH when the schematic intended a grounded PTH (or vice versa) breaks the chassis-ground/EMI-shield path decided at schematic time. |
| paste-aperture-thermal-pad | fab | judgment | A thermal-pad paste window follows a generic percentage instead of the datasheet-recommended pattern, causing solder voiding or poor thermal/electrical contact under a QFN-style part. |
| fiducials-panelization-jlc | fab | judgment | An unusual outline or a fiducial JLC's standard prototype flow doesn't expect stalls the order or cracks boards at depanelization. Applies: boards needing non-standard panelization. |
| assembly-drawing-consistency | fab | judgment | A DNP note or polarity/orientation mark is missing or inconsistent between schematic, layout, and assembly drawing, so assembly places or populates a part wrong even though it "matches the Gerbers." |
| fastener-galvanic-compatibility | fab | judgment | A screw/standoff/nut pairing of dissimilar metals corrodes or seizes in service (worse in humid/outdoor use). |
| bringup-procedure-written | fab | judgment | No step-by-step bring-up procedure (power-up order, what to measure where with expected values, when to flash which firmware) written before boards ship means missing provisions — a test point, a rail sense, a current-limit setting — surface with the board in hand instead of while they could still be added. Deliberately a forcing function: the procedure will change at bring-up; *authoring the walk-through* is what surfaces the gaps, and its evidence anchor is the procedure document itself. |

## Discarded

- **Bare DRC/ERC rule restatements** — courtyard clearance numbers, trace-width-vs-current mil/amp rule, solder-mask expansion/sliver minimums, paste-aperture minimums, drill annular-ring/hole-tolerance minimums, board-edge clearance numbers, thermal-relief spoke-width minimum, generic fiducial size/clearance numbers. All encodable geometric rules — they collapse into `drc-clean`; a standalone item would just decompose the coarse tool item, which the design explicitly forbids.
- **Process/culture prose** — "multi-pass review culture" (self/peer/specialist review passes), generic "DFM/DFA collaboration with the CM" advice. Not a checkable item on a target; it's how humans historically ran review, not a failure the checklist can verify against a board.
- **Generic "DFM/DFA rules" as a top-level item** — for this stack, JLC's fab constraints are already the constraints encoded in `drc-clean`; a separate item would just restate that tool item.
- **Full custom panelization/fiducial planning as a universal requirement** — the report treats it as standard practice, but JLC auto-panelizes standard small-board prototype runs; kept only as a conditional (`applies`) item, not a blanket one.
- **LED indicators for power/comm/fault state** — soft diagnostic convenience, not a hard functional failure; the report itself only weakly motivates it. No concrete "prevents" survives scrutiny, so discarded per the cargo-cult filter.
- **Controlled-impedance/diff-pair target number as its own layout item** — the number itself is DRC-rule-shaped once a stackup is fixed (tool-checkable); the only real judgment risk is whether the *as-fabbed* stackup matches the design assumption, which is `stackup-vs-fab-capability` in the fab phase — kept once, not twice.

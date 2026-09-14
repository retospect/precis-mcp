---
status: draft
title: EWOD synthesis protocol layer — functional zones, se-bound mechanics, route→droplet-schedule compiler
prio: normal
blocked-by: pcb-ewod-multitile
---

# EWOD synthesis protocol layer — functional zones, se-bound mechanics, route→droplet-schedule compiler

Vision (Reto, 2026-09-14): tie the EWOD board
(`pcb-ewod-multitile.md`) to the chemistry synthesis path with an
se-like abstraction. We have a synthesis path (`route` kind, already
`constraints=['ewod-oil']`-aware), reagents on the PCB, a synthesis
target, magnetic beads, and a set of **functional zones** on the pad
field: switchable magnetic zones (a motor moves a permanent magnet up
and down beneath the board), heated zones, chilled Peltier zones,
inlets, outlets, and illumination sites at various wavelengths.
Reactions happen at these spots. The protocol layer routes droplets
through the synthesis path over these zones — keeping droplet travel
short and cross-contamination low — and reports feasibility in phases
like the optimizer does ("area too small", "too many magnet zones for
the below-deck packing"). Zones can be **cleaned and reused**.

## Architecture — four layers, each with an owner

1. **Functional-zone model (pcb kind).** A `zone` is a named region of
   the pad field with a capability:
   `reservoir(reagent) | inlet | outlet/waste | magnet | heat | chill |
   illuminate(wavelengths) | react (plain)`. Zones are emitted by
   generators / pre-place-route blocks (magnet-motor block, Peltier
   zone, heater block, dispenser, illumination site) into the
   generator **ledger**, with pad membership and capability params
   (temp range, wavelengths, actuation). `view='capability'` renders
   zone overlays. Zones are layout-time facts; the pcb kind stays
   protocol-ignorant.
2. **se binding (mechanical feasibility).** Below-deck hardware —
   magnet z-motors, Peltiers, sink ICs, sensors, illumination
   hardware — competes for the bottom face and vertical clearance.
   `pcb-se-binding.md` (main, 2026-09-14) already rules the seam: pcb
   is a mm enclave, `mechanical_profile` is the sole geometry
   crossing, se consumes board segments + keep-out prisms one-way.
   This layer extends the profile with per-zone below-board actuator
   envelopes (motor travel volumes, Peltier + heatsink stacks) so
   **se answers the packing questions** — how close can magnet motors
   sit, can a Peltier and a magnet motor share a tile neighbourhood,
   z-travel clearance — and the verdicts come back as zone-placement
   constraints ("too many magnet zones for this pitch").
3. **Protocol IR + compiler (new, chem-facing).** Input: a `route`
   with **structured step conditions** (solvent, temperature,
   atmosphere — item (1) of `chem-tools-integration.md` §Platform
   constraints; lexical screening is today's ceiling and is not
   compilable), the target, and the board's zone ledger. Output: a
   timed **droplet schedule** — dispense / merge / move / hold(zone,
   time, T, field, λ) / bead-capture / wash / eject — over the zone
   graph. Objectives: short travel paths, low cross-contamination,
   zone-contention scheduling. Like the optimizer's phases, the
   compiler returns **verdicts, not just failures**: area too small,
   too few magnet zones, reservoir count insufficient, schedule
   infeasible at requested parallelism — each with the resize/add
   suggestion that would clear it.
4. **Contamination + cleaning model.** Every pad carries a hygiene
   state per substance class (touched-by ledger). Droplet transit
   marks; a wash cycle (wash droplet + oil flush to waste) resets with
   a residual score. The compiler avoids routing incompatible
   chemistries over marked pads OR schedules cleaning and reuses the
   area — reuse is allowed, priced by wash time and residual risk.
   Incompatibility classes seed from `precis_chem.constraints`
   (EWOD_OIL solvent lists) until something better exists.

## Magnetic beads (why magnet zones exist)

Standard DMF solid-phase pattern: functionalized beads in a droplet;
magnet UP pins the bead pellet against the board through the oil gap
while EWOD splits the supernatant away; magnet DOWN releases for
resuspension. Gives capture / wash / elute cycles — the workhorse for
purification between synthesis steps and for the target's final
cleanup. The zone needs: z-actuated magnet below (motor per zone — a
below-deck envelope for se), a pad group sized for pellet + wash
geometry, and a waste path that does not cross clean lanes.

## In scope (slices)

- **A — zone model + ledger/capability-map extension** (pcb kind;
  builds directly on the multitile generator's ledger). Zones as
  authored/generator-emitted annotations; no behavior.
- **B — below-deck envelopes into the se crossing** (blocked-by:
  `pcb-se-binding` landing, which is itself sequenced behind
  nm-se-merge). Actuator/Peltier/motor volumes into
  `mechanical_profile` segments; se-side packing checks + verdicts.
- **C — protocol IR + compiler MVP** (blocked-by: structured
  `RouteStep.conditions`). Greedy list-scheduler over the zone graph;
  travel + contention only; verdict reporting. No optimization pass.
- **D — contamination/wash model + area reuse.** Hygiene ledger, wash
  op, residual scoring, incompatibility classes; compiler integration.
- **E — feasibility loop with route/optimizer phases.** Verdicts
  round-trip: compiler verdicts surface on the route (annotating steps
  with zone assignments or infeasibility), and board-resize
  suggestions reference generator params (grid, sink_grid, zone
  counts).

## Explicitly NOT in scope

- Firmware / real-time droplet control, feedback execution — the
  schedule is a plan artifact; an executor is future work.
- Chemistry validation of steps (the route kind owns that).
- Illumination hardware design (delivery path undecided — see open
  questions).
- Modifying se's block graph semantics — the binding stays one-way per
  `pcb-se-binding.md`.

## Acceptance criteria (draft-level; firm up per slice at ready time)

1. A dogfood-board ledger + a 3-step toy route (dispense A, heat-hold,
   bead-wash, elute to outlet) compiles to a schedule that renders as
   a timeline + path overlay on the capability map, with zero
   incompatible-transit violations.
2. An over-constrained input (more simultaneous heated holds than
   heater zones) returns a verdict naming the shortage and the
   generator param that would clear it — not a bare failure.
3. A washed-and-reused pad path is chosen when it beats the detour, and
   the schedule shows the wash op explicitly.
4. se packing check rejects a magnet-motor layout that collides
   below-deck envelopes, and the verdict names the offending zones.

## Open questions / decisions log

Decided (Reto, 2026-09-14):

- **Illumination = a top PCB with LEDs in spots**, possibly
  trans-illumination through **ITO inserts** (transparent windows in
  the lid board that keep the ground function while passing light).
  Consequence: the top plate becomes a second pcb-kind board — LED
  sites mirroring bottom-board zones, ITO window cutouts (needs the
  outline-cutout work parked in `pcb-card-edge-outline.md`), the
  ground/complement-drive terminal, and a loose (~zone-scale)
  registration constraint to the bottom board. The lid board is a
  design object of this programme, not an off-the-shelf ITO slide.
- **Magnet actuation style is the implementer's choice.** The model
  never encodes mechanism — it tracks each zone's **size, effect, and
  localized capability declaratively** ("dry"): below-deck envelope
  dims, field on/off effect at the pad plane, switching time, duty
  limits. The zone capability schema (slice A) must be expressive
  enough that servo-cam, voice-coil, and gantry implementations all
  describe themselves in the same fields; a gantry additionally
  declares shared-actuator exclusivity (a scheduling constraint, not
  hardware modeling).
- **Bead + protocol source**: EWOD bead and protocol information
  already exists in the compute-boxel draft (`draft:nano-computer`,
  prod). Slice C mines that draft FIRST; fresh lit search only for
  what it doesn't cover.

Still open:

- **Wavelength set** for the LED spots (and whether any site needs
  power-class LEDs with their own thermal budget on the lid).
- **Bead handling params** not covered by the boxel draft: pellet
  pad-count, retention vs oil gap, wash-count defaults.
- **Scheduler formalism**: greedy list scheduler first (slice C);
  upgrade path (CP/SAT, simulated annealing shared with the pcb
  optimizer) only if verdict quality demands it.
- **Where the protocol executes** eventually: cluster agent driving
  serial over the sink chain vs on-board MCU — deferred with the
  executor.
- **Hygiene ledger granularity**: per-pad per-class booleans vs
  residual scalar; start boolean + wash-count.

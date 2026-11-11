---
status: draft
title: magnetomechanical memory in carbon nanobuds — intrinsic magnetic order coupled to structural state
model: opus
pillar: 3d-design
blocked-by: nanobud-campaign
---

# Nanobud magnetomechanical memory

*(Transfer to graph memory is pending — spec-in-memory ruling
`me568168`, gripe `gr629126`. Owner after reading: `me628837`
(boxel-assembly memory/actuation) is the relating hub named in the
gripe, but this doc's own Target is spec `me629936` (nanobuds
mechanical architecture) — the executing lane picks one `part-of`
and links the other. Both 2026-11-11 transfer passes had no precis
MCP; the executing lane needs the session MCP on PROD — the writes
cannot be done from a no-MCP lane. Do not delete this file until the
nodes below are written, the hub reading order carries their handles,
and the references are repointed — retiring ahead of the transfer was
reverted once already (`a331c87a4`).)*

*Transfer manifest — coverage unchecked (this lane had no MCP; the
executing lane searches the graph first and folds in what already
exists). Write each as one `section:reference` node, `part-of` → the
owning hub, `Source:` line naming this file + transfer date:*

1. *Title ≈ "Mechanical-write, magnetic-read cell on nanobud order".
   Body: Reto 2026-09-14 — explore whether magnetic state in an
   all-carbon nanostructure can couple to structural configuration,
   giving mechanical write + magnetic read (or vice versa). Seed
   observation: fi189548 (dr173020) asserts intrinsic magnetic
   moments with no magnetising step — order from topology, not
   ferromagnetic elements. Motivation: the nanobuds architecture has
   no high-density purely-mechanical storage layer; one substrate
   would give write (actuate) + read (sense) with no extrinsic
   magnetic material.*
2. *Title ≈ "Scope: validate order, bistability, energetics, cell
   sketch". Body: in scope — establish whether fi189548's magnetism
   is genuine local order (not impurity / spin contamination /
   measurement noise) and its Curie temperature; whether the state is
   bistable and mechanically coupled to a structural degree of
   freedom; write/read energies + retention; a first-pass write/read
   cell (actuator type, sensing modality, cycle life). Out of scope —
   new synthesis or experiment on nanobuds; competing magnetic
   substrates (the rare-earth benchmark is a separate research pass);
   device integration/fabrication; thermal management unless the
   Curie temperature forces it. Acceptance: genuine-order review; Tc
   determined (below ambient kills the premise); a reversible
   mechanically-coupled DOF identified; order-of-magnitude write/read
   energies; a cell sketch (bits per nanobud; actuator — STM tip,
   electrostatic, photonic; magnetometry type; cycle life).*
3. *Title ≈ "fi189548's sign-off gates the memory premise". Body:
   parked behind nanobud-campaign's re-review of fi189548 (Reto
   2026-10-21) — that re-review IS the magnetism validation, no
   separate item. Open questions: real order vs artefact; Tc vs
   300 K — the single most-gating number; which structural DOF
   couples and is it bistable (C–C bond-bending, point-defect
   mobility, larger distortion — must reverse without bond rupture);
   write-energy floor vs tolerated actuation; the
   density-vs-cycle-life sweet spot. Blast radius: spec `me629936`
   (structural state / envelope work), the dr173020/fi189548 claim
   corpus, the concurrent rare-earth evaluation, a future
   storage-layer subsystem. `cites`/`qualifies` → `me629936` and the
   campaign node.*

*Then: append the new handles to the owning hub's reading order;
repoint `docs/backlog/threads/nanobuds-paper.md` Horizon 6 and this
file's `blocked-by:` front-matter (to the campaign node's handle);
delete this file and regenerate `docs/backlog/INDEX.md`
(`scripts/docs-index`).*

Reto, 2026-09-14: explore whether magnetic state in an all-carbon nanostructure can be mechanically coupled to mechanical/structural configuration, enabling mechanical write + magnetic read (or vice versa).

Seed observation: claim `fi189548` in draft `dr173020` ("Carbon Nanobuds at the Interface of Theory and Experiment") asserts intrinsic magnetic moments in an all-carbon nanostructure **without any magnetising step** — i.e. magnetic order arising from topology/structure rather than from ferromagnetic elements. The conjecture: if magnetic state is genuinely tied to structural configuration, a memory element could in principle be written by mechanical means and read magnetically.

## Motivation / why

The nanobuds architecture presently lacks a high-density, purely mechanical storage layer. Magnetism from an intrinsic carbon nanostructure — if bistable and mechanically coupled — would offer a write mechanism (actuate the structure) and read mechanism (sense magnetic state) from the same physical substrate, without extrinsic magnetic materials.

## In scope

- **Validate the premise.** Establish whether the magnetism reported in fi189548 is genuine magnetic order (not measurement artefact, not impurity) and the Curie temperature.
- **Bistability check.** Determine whether magnetic state is actually bistable and mechanically coupled to a structural degree of freedom (e.g. does a specific deformation or conformational change reversibly flip magnetic state).
- **Energetics sketch.** Estimate write energy (structural deformation cost), read energy (magnetic sensing cost), and retention time at relevant operating temperatures.
- **Design implications.** If the preceding hold, outline what a write/read cell would look like: actuator type, sensing modality, cycle count before degradation.
- **First cheap investigation.** Scoped below.

## Explicitly NOT in scope

- **New synthesis or experimental work on nanobuds themselves.** This is a computational and literature study over existing observations.
- **Competing magnetic substrates.** No comparative study against rare-earth magnets, hard ferrites, or other magnetic materials; the rare-earth benchmark work is a **concurrent research pass**, not part of this scope.
- **Device integration or fabrication.** Assumes the nanobud itself is available; does not scope packaging, addressing, or integration into a larger array.
- **Thermal management.** Assumes ambient operation; cryogenic or controlled-temperature scenarios are out of scope unless the Curie temperature forces them.

## Acceptance criteria

1. A literature and computational review establishes whether fi189548's magnetism is **genuine local magnetic order** (distinguishing from impurity contributions, spin contamination, or measurement noise).
2. **Curie temperature is determined** (from the literature or a small computational screening) — if it is below ambient, the premise is dead; if it is above ambient, proceed to (3).
3. A plausible **mechanically-coupled degree of freedom** is identified — a bond rotation, defect migration, or structural distortion that **reversibly** changes magnetic state without breaking the nanostructure (i.e. bistable, not destructive).
4. **Rough estimates of write and read energies** are sketched (mechanical actuation cost, magnetic sensing noise floor) — order-of-magnitude suffices at this stage.
5. A **first-pass design sketch** exists: how many bits per nanobud, actuator type (e.g. STM tip, electrostatic, photonic), sensing modality (magnetometry type), expected cycle life.

## Target + blast radius

- **The nanobuds mechanical architecture** (spec `me629936`, structural state / envelope work).
- **The knowledge base** (`dr173020` and the fi189548 claim corpus).
- **Concurrent rare-earth-magnet evaluation** — this item feeds into and is fed by the magnetism-claim verification.
- **Future storage-layer design** — if the premise holds, informs a later mechanical-actuator + magnetic-storage subsystem build.

## Decisions (Reto 2026-10-21)

- Parked behind `nanobud-campaign.md`'s sign-off of fi189548: the
  campaign's re-review of fi189548 is the magnetism validation. No split
  into a separate validation item.

## Open questions / decisions log

- **Is fi189548's reported magnetism real order or an artefact?** Concurrent research is grounding this against rare-earth-magnet benchmarks; this item waits for confirmation.
- **Curie temperature — above or below ambient?** The single-most-gating number. If Tc < 300 K, the mechanism doesn't work at room temperature without environmental control.
- **Which structural degree of freedom couples to magnetism, and is it bistable?** Candidates include bond-bending (C–C rotation in the nanobud cage), point-defect mobility (carbon vacancy or topological defect), or larger-scale distortions. Must reverse without breaking the structure (i.e. no net chemical change, no bond rupture).
- **What write energy is acceptable?** Mechanical actuation (STM, AFM, voltage, photon) comes with a floor; does it overlap with energies the nanobud can tolerate without thermal runaway or plasticity?
- **Cycle life vs. storage density trade-off.** Higher bit density (more magnetic sites per nanobud) may increase cross-talk or reduce cycle life. Where is the sweet spot?

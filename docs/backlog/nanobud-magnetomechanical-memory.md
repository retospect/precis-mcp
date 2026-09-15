---
status: draft
title: magnetomechanical memory in carbon nanobuds — intrinsic magnetic order coupled to structural state
model: opus
---

# Nanobud magnetomechanical memory

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

- **The nanobuds mechanical architecture** (`docs/backlog/nm-kind.md`, structural state / envelope work).
- **The knowledge base** (`dr173020` and the fi189548 claim corpus).
- **Concurrent rare-earth-magnet evaluation** — this item feeds into and is fed by the magnetism-claim verification.
- **Future storage-layer design** — if the premise holds, informs a later mechanical-actuator + magnetic-storage subsystem build.

## Open questions / decisions log

- **Is fi189548's reported magnetism real order or an artefact?** Concurrent research is grounding this against rare-earth-magnet benchmarks; this item waits for confirmation.
- **Curie temperature — above or below ambient?** The single-most-gating number. If Tc < 300 K, the mechanism doesn't work at room temperature without environmental control.
- **Which structural degree of freedom couples to magnetism, and is it bistable?** Candidates include bond-bending (C–C rotation in the nanobud cage), point-defect mobility (carbon vacancy or topological defect), or larger-scale distortions. Must reverse without breaking the structure (i.e. no net chemical change, no bond rupture).
- **What write energy is acceptable?** Mechanical actuation (STM, AFM, voltage, photon) comes with a floor; does it overlap with energies the nanobud can tolerate without thermal runaway or plasticity?
- **Cycle life vs. storage density trade-off.** Higher bit density (more magnetic sites per nanobud) may increase cross-talk or reduce cycle life. Where is the sweet spot?
- **Split into two investigations.** Validating the magnetism (items 1–2 above, a 2–4 week literature + shallow computation pass) is independent of the coupling investigation (items 3–5, a longer structural + energetics screening). If the magnetism fails (Tc is low, or it is an artefact), the coupling work is moot. Consider splitting into `nanobud-magnetism-validation` (ready first) and `nanobud-magnetomechanical-mechanism` (blocked on magnetism confirmation) if both are priorities.

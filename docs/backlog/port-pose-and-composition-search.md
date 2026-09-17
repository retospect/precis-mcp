---
status: draft
title: port pose slot + requirement-driven composition search ("10–12 Å over 40–50 nm")
prio: high
model: opus
---

# Port pose slot + composition search

Reto, 2026-09-16, settling gr342026 and extending
`blocktree-library-build-plan.md` §Slice 4. Three decisions, one new
design item.

## Decision 1 — ports get a pose slot, on the port, as a *target* — **SHIPPED**

Ruling (gr342026): **put it on the port**, mirroring the block's own pose
— an origin and a rotation, so a state's `port_pose_overrides` is a
**rigid delta in the block frame** (translation + rotation), not
direction-only. Delta, not absolute, because it stays meaningful when the
port's own pose is unknown ("the far port moves 9 Å along x").

Shipped 2026-09-17 (declared half): `Port.pose`/`rot`/`pose_source`
(`precis.blocktree.types`, enum `PORT_POSE_SOURCES = declared | bound`,
`None` = unset); `add_port pose= rot=` and the new `set_port_pose` op
(rot without an origin refused, scalar refused); se columns
`se_ports.pose_xyz/pose_rot/pose_source` with CHECKs
(`0011_se_port_pose.sql`, metres/radians, block-local frame);
`port_pose_overrides = {port: {'direction'?, 'pose'?, 'rot'?}}` applied
at get time only when the port carries a pose (sweep resets it per
combo); a `pose` column on `view='block'|'ports'` only when some port
has one; `bond_length_sanity` uses the port-to-port world distance when
both ends carry a pose and says which source each came from, else the
envelope approximation and says so.

**Why rotation too:** a rotation at a hinge port × arm length is a
displacement at the far end. Without the angle the solver below can only
find series stacking; with it, levers/cranks/scissors are solutions.

**Open — the `bound` half:** the enum value, CHECK and render exist, but
nothing writes `pose_source='bound'`. Natural writer is `bind_structure`
(it already resolves each port to an atom; the atom's block-local
coordinates are the pose). Reto's call: does a bind overwrite a
`declared` target, or refuse and file a mismatch finding? Waits on
per-state `bind_structure`. Intervals on a target ("10–12 Å") are
Decision 3's requirement, not the port slot.

## Decision 2 — Δ-length facts stay in the star schema

Unchanged from §Slice 4: Δ-distance, PSS conversion, τ½, strut stiffness
are sourced rows with conditions + citation on what the block is
`realized-by`, never denormalised onto the block. A port target is a
*requirement*; the star-schema row is the *fact*; ranked search compares
the two.

## Decision 3 — a requirement is a box with ports and interval constraints

Declare a block with two ports and a state pair; on the transition put
ranges: Δ between ports ∈ [10, 12] Å, span ∈ [40, 50] nm, stimulus
`light`, bistable preferred, cycles ≥ N. Declared intent only.

## New item — composition proposer (se atomic `propose` mode)

Reads slice 4's rows and enumerates compositions that satisfy the
requirement: series of n switches + spacers (n·Δ_unit ∈ Δ-range,
n·L_switch + spacers ∈ span-range), and lever/hinge families once ports
carry rotation. Small integer enumeration. Output = slice 4's response
shape extended to compositions, ranked, never empty:

    3 × azobenzene in series + 2 × spacer: Δ 10.2 Å (8.2 Å at PSS 80 % cis)
    span 44 nm · opto ✓ · bistable ✗ (T-type, τ½ 2 d) · CuAAC ✓

A chosen row instantiates as an se tree of library instances joined by
complementary click ports (slice 3 makes azide↔alkyne legal and
azide–azide refused); unit states compose into chain states the sweep
view already enumerates; selection reuses quest's rubric machinery with
human-set weights. DRC (slices 5/6) then runs on the composed tree.

**Must surface, never hide:** PSS conversion < 100 % (expected Δ =
n·Δ·p_cis, shown per row); a 40–50 nm span exceeds rigid small-organic
struts, so the spacer library (DNA / peptide / OPE rods with stiffness
rows) decides whether a series stroke survives.

## Order

1. Port pose slot (decision 1) — before slice 4 or a consumer bakes in
   direction-only semantics (`nm-stick-placement.md`,
   `structural-solution-space.md` slice 5).
2. Slice 4 ranked search, with the star-schema shape (decision 2).
3. Composition proposer.

Sources to import before the proposer ships (cite-sources rule): azobenzene
trans/cis Δ end-to-end and PSS ratios; OPE / dsDNA persistence lengths.

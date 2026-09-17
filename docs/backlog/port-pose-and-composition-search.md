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

## Decision 1 — ports get a pose slot, on the port, as a *target*

gr342026: a state can re-aim a port (`direction`) but not move it, because
`Port` has no position. Ruling: **put it on the port**, mirroring the
block's own pose — an origin and a rotation, so a state's
`port_pose_overrides` becomes a **rigid delta in the block frame**
(translation + rotation), not direction-only.

The slot is **nullable and provenance-tagged**, because at the box level
the exact displacement is not known up front — it depends on the
realization. The value is one of: a *declared target* (design intent,
intervals allowed), a *bound geometry* (filled later from the realized
`structure` per state, the `expected_element`/`bound_atom` pattern), or
unset. A bare float with no origin is refused. Every consumer says which
it used: clearance/sweep/bond-length keep today's approximation (block
pose + envelope extent, `precis_se/atomic/validate.py`'s "ports have no
stored position" comment) when the slot is null.

**Why rotation too:** a rotation at a hinge port × arm length is a
displacement at the far end. Without the angle the solver below can only
find series stacking; with it, levers/cranks/scissors are solutions.

Cost: one nullable column in design core (migration), persist round-trip,
one more key next to `direction` in `_vet_port_pose_overrides`, a small
provenance enum. Bound half waits for per-state `bind_structure`.

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

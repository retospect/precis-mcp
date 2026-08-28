---
status: draft
title: Constraint classes the place+route model is missing (survey)
prio: high
model: opus
---

# Missing constraint classes — survey

Prompted by "what other things did we miss?" (2026-08-28), after silkscreen
labelling turned out to be a whole constraint class nobody had modelled.
This is the systematic sweep. Not all of it is worth building; the point is
that none of it should be discovered during a board bring-up.

Ordered by how badly each bites.

## A. Board does not work, or cannot be assembled

**Fiducials — absent entirely.** Pick-and-place needs them (typically 3 per
populated side, unobstructed, with their own clearance and no silk/mask
intrusion). JLC requires them for fine-pitch parts. This is a hard assembly
blocker, not a refinement.

**Thermal relief on plane connections.** A pad tied solidly into a pour
wicks heat away and will not solder reliably — it needs spoked relief. The
tiling/pour pass must know which pads get relief and which need a solid tie
(high-current ones do).

**Shunt-device placement (NOT a new constraint class).** An earlier draft
claimed topological ordering as a missing class. Mostly wrong — corrected
2026-08-28:
- **Series elements** (fuse, common-mode choke, series R) *are*
  netlist-implied: connector → net A → fuse → net B → load. Two distinct
  nets; ordering falls out. Nothing needed.
- **Kelvin/sense** is likewise netlist-implied when drawn correctly — the
  sense line is its own net.
- **Shunt elements are the real case**: a TVS or ESD diode puts the
  connector pin, the protection pin and the load pin all on **one net**,
  so the netlist cannot say whether the route passes *through* the
  protection or merely stubs off to it. Physically it must pass through,
  with a short stub.

That is the **bypass-cap problem already solved** — a shunt device whose
position matters, expressed as a per-connection objective. Reuse that
machinery; do not build an "X lies on the path A→B" constraint system.

Board-readability ordering (parts arranged left-to-right like a schematic)
is a nice-to-have and **not worth the cost** — explicitly declined.

**Crystals/oscillators** — keepout under the part, short traces, guard
ring, no routing beneath.

**⚠ ARMED, NOT YET FIRING: pads with no electrical function.** A
through-hole mechanical part (mounting nut, standoff, non-electrical
connector shell) has pads on **no net**. `ir.unconnected_items()` flags
every pin with `pin_net == NO_NET` as `"unconnected-pin"`, so each such
part emits spurious findings on every run.

Verified 2026-08-28: `unconnected_items` has **no production consumer** —
only `tests/test_pcb_ir.py` references it. So there is no live bug *today*.
But it is plainly meant to be wired up (it is the graph-feasibility half of
the retired `drc_lite`), and the moment it backs a view or a gate, every
mechanical part produces false findings — and a gate consuming it wedges,
exactly as `route_complete` did on dangling nets.

Root defect is the **pad-level twin of the dangling-net bug**: "not
connected" has two meanings — *shouldn't be* vs. *should be but isn't* —
and the code conflates them. Fix before wiring: mark pads with no
electrical function (from part/footprint data), and exclude them from the
unconnected report, from escape-routing demand, and from layer-count
derivation. Fixing it now, while nothing consumes it, is nearly free.

## B. Cost, sometimes dramatically

**Single-side assembly — a STEP function, not per-part.** Populating one
side only is markedly cheaper (second reflow pass, second stencil, second
P&P setup). The cost is `any part on the back → setup cost`: one part or
fifty, identical. Do **not** model it per-part.

Cheap to evaluate incrementally: keep a counter of back-side parts; the
term flips only as the counter crosses 0↔1, so the delta stays O(1).

**⚠ Reachability problem.** With a step cost the *first* part moved to the
back pays the whole setup penalty with no offsetting benefit, so
single-move SA will essentially never accept it and two-sided layouts
become unreachable — the same failure shape as the snap term in §C.
Do not fix this with a compound move; **optimize one-sided and two-sided
as separate configurations and compare totals**, which also matches how
the decision is actually made.

This would give `SIDE_FLIP` a **real cost** and retire one of the three
known-inert move classes recorded in the master spec.

**Panel price breaks are a step function, not linear area.** Fabs price in
tiers (100×100 mm is a common cliff). The `board_area` term currently
scores linearly, so it will happily spend 20% more area for no cost
signal — and equally will not fight to stay under a cliff it cannot see.

**Unique part count.** Consolidating resistor/capacitor values reduces BOM
lines, feeder slots and assembly setup. A real optimization the model
cannot currently express.

**Via-in-pad** needs fill-and-plate — flag it as a cost escalation rather
than letting the router use it freely.

## C. Coordinate "CAD-friendliness" — with a trap

Mounting holes, connectors and board outline should land on round mm
coordinates. The user's preference ordering (50 > 40 > 55 > 57.323) is
exactly **"the coarsest grid the value lands on"**: 50 → 25 mm grid, 40 →
10 mm, 55 → 5 mm, 57.323 → none. Cheap discrete ladder, cheap to score.

**⚠ A cost term alone is unreachable.** Continuous translation moves land
exactly on 25.000 mm with probability zero, so the term would sit there
never firing — the same shape as the crossings estimator that was provably
always zero. **This requires an explicit SNAP move class** proposing the
nearby grid point. Do not ship the term without the move.

Extensions:
- **Hole-pattern pitch matters more than absolute position** — 50 mm
  between holes beats each hole being individually round.
- **Board outline dimensions snap too** — 50×70, not 51.3×68.7.

### Regularity (arrays, aligned parts) — comes free from SNAP

We mildly prefer regular arrangements: aligned parts, even spacing, labels
on a common side. **Do not add a separate regularity objective**, and do
not expect it to emerge — nothing in loop area, thermal or crossings
rewards alignment, so "see what emerges" yields nothing.

Grid snapping already produces it: parts snapped to a coarse grid are
aligned and evenly spaced *by construction*. One mechanism, two payoffs
(CAD-friendliness and visual regularity).

**The general rule this is an instance of — worth internalizing, it has
bitten three times:**

> Whether a preference can be a pure cost term depends on whether its
> variable is **discrete or continuous**. Discrete → the term fires.
> Continuous → the term is measure-zero and needs a move class.

Applications: *component coordinates* are continuous ⇒ alignment and
round-number preferences need the SNAP move. *Label side* is discrete (a
small candidate set per part) ⇒ "agree with your group's dominant side"
works as a plain cost term. The three instances of getting this wrong:
the crossings estimator that was provably always zero, the round-number
term, and regularity.

### Subcircuit replication (stronger than regularity)

For repeated blocks — the Nano board's four rail-LED pairs, its three
near-identical bucks — **solve one placement and replicate it with an
offset**, rather than asking SA to independently rediscover the same
arrangement N times. Gives exact regularity *and* collapses the search
space. Analogue of cloning in IC placement.

## C2. Constraint lifecycle: optimized → locked (enclosure handoff)

The intended workflow: **v1 lets screws and mounting holes float** and
optimizes them; the enclosure is then built from that result; the positions
are **locked** for every subsequent revision, because a physical artifact
now depends on them.

Most of the machinery exists and is simply not named as a lifecycle:
- `inst_fixed_xy` / `inst_fixed_rot` already give per-object pinning
  (mounting holes need the same treatment).
- `view='mechanical'` already exists as an export — it is the handoff
  artifact (hole pattern, outline, connector positions).

So: optimize → `view='mechanical'` → CAD/enclosure → lock. §C's round-number
snapping is what makes the middle step tolerable; the lock is what makes it
permanent. They are one story, not two features.

**Two additions so locks do not silently rot the board:**

1. **Record *why*, with provenance.** "fixed at (25.0, 40.0)" is far less
   useful than "locked to enclosure rev B, 2026-09-xx". Three revisions
   later someone must be able to tell whether unlocking costs a new
   enclosure or costs nothing.
2. **Report each lock's shadow price.** We have a cost function, so measure
   the counterfactual: optimize with the lock, optimize without, report the
   delta. That turns "these four holes are fixed" into "these four holes
   are costing you a layer" — the input needed to decide whether respinning
   the enclosure is worth it. Without it, locks accumulate silently and
   every revision degrades with no visible cause.

## D. Mechanical / integration

**Solder-on standoff nuts are NOT a special case** (e.g. Sinhoo
C2916373). They have footprints, so they are just parts — pads, courtyard,
keepout and hole all come from footprint data we already parse. "A kind of
via": a plated hole with copper that spans layers and obstructs routing,
which the existing via/keepout obstacle unification already covers.
Chassis-ground vs. isolated is a net assignment, nothing more. *(Recorded
because an earlier draft of this file over-modelled them — do not
reintroduce bespoke handling.)*

The one residual is **generic, not nut-specific**: a mechanical keepout may
extend to the **opposite side** (screw head + washer clearance), which a
footprint does not describe. Applies to any through-hole mechanical part.

**Connector mating axis + enclosure cutout alignment.** "Near an edge" is
insufficient — the mating direction must be perpendicular to a specific
edge at a specific offset, or the cutout misses.

**Depanelization stress.** MLCCs crack near V-scores; keep ceramics and
other brittle parts off break edges. Note board-edge clearance is already
known not to be a scalar (0.2 mm routed vs 0.4 mm V-cut).

**Component height zones.** `height_mm` already exists in the graph dict
and is unused for clearance — lid/display fit needs it.

**Test points** for bring-up; probe access and spacing.

**IO side-gravity.** Soft directional bias for unfixed IO toward a chosen
edge; composes with the mating-axis constraint above.

## E0. HV slots and creepage — a second distance metric

HV cutout gaps (milled slots between high-voltage nets) are **akin to but
distinct from** guard traces:

| | Guard trace | HV slot |
|---|---|---|
| Nature | *additive* copper, on a net | *subtractive* substrate, on no net |
| Extent | one layer | through all layers |
| Purpose | intercept leakage at a defined potential | lengthen the creepage path |

**The obstacle half is already covered** — a slot is an internal cutout,
i.e. the existing layer-masked obstacle primitive with the mask set to all
layers. Nothing routes through it.

**The metric half is missing.** Clearance is a straight-line (through-air)
measurement; **creepage is a geodesic** — shortest path *along the
surface*, which must detour around a slot. A slot can leave Euclidean
clearance unchanged while doubling creepage, so a check that only measures
straight-line distance sees no benefit from the exact feature added to
gain one, and cannot tell you when one is required.

Thresholds come from working voltage, pollution degree and material group
(IEC 60664 / 61010) — the per-net rule resolver (stage 1 of the gap work)
is the natural home for those. The **distance function itself is new
work**.

**Not needed for the USB-C PD Nano board** — creepage requirements at 20 V
are trivial. Recorded so it is not built speculatively; revisit if a
mains-adjacent or high-voltage board is ever attempted.

## E. Electrical, beyond what is modelled

- **Plane-split return paths** — a signal crossing a split has no return;
  classic silent failure.
- **Star / single-point grounding** for mixed analog-digital.
- **Bulk + inrush capacitor placement.**
- **Polarity and pin-1 orientation marks** on silk — assembly correctness
  (ties into the silkscreen work).

## Sequencing

None of this blocks the current gap work (per-net width/clearance, via
geometry). Pick from A first — those are correctness, not polish. B is the
cheapest real win (single-side assembly also retires an inert move class).
C is small but must ship the SNAP move with the term.

> **Consider for the paper.** Two items here are paper material: the
> unreachable-cost-term trap (a term a continuous move class can never
> satisfy is indistinguishable from a working one — the third instance of
> this failure mode in this build), and topological ordering constraints as
> a class that connectivity-based verification structurally cannot catch.
> Collected in `pcb-paper-benchmark-selection.md` §CONSIDER FOR THE PAPER.

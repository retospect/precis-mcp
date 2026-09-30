---
status: draft
title: The placer leaves a 0.093mm gap where 55 nets need 16.5mm, and the failure reports as a routing problem
prio: normal
pillar: 3d-design
---

# The placer starves the escape corridor

## Motivation / why

On the EWOD dogfood at seed 1 with In2.Cu open (the 50/54 escape run),
all four failing escape nets record the same first-listed problem, byte
for byte:

```
gap 0.093 mm between instances (0, 1, 7) fits 0 strand(s) at 0.300 mm
pitch, but 55 want through (ARR1_R0C2 … ARR1_RESV, VDD_LOGIC)
— needs 16.500 mm
```

Fifty-five nets want through a gap that admits **zero** strands. The
router is being asked to do something arithmetically impossible and is
correctly reporting that it cannot, but the report arrives as four
per-net routing failures (`congestion` ×2, `no_path` ×2) rather than as
"the placement does not leave room for the escape fan". The proximate
cause is 16.4 mm of missing clearance between three instances.

**Why this is worth its own item rather than a line under routing.** The
measurement that found it (2026-09-28, recorded under item 3 of
`pcb-escape-and-driver-chain.md`) was aimed at a routing question — do
realized escape paths cross, i.e. is pin-swap ordering worth building?
The answer was yes (51 swappable crossings over 41 net pairs), but the
four actual failures turned out not to be crossing-limited at all. Those
are two separate levers, and conflating them would let a successful
swap-ordering round look like a failure because the yield did not move.

It is also the same defect shape this campaign keeps finding: a
constraint that a pass cannot see is one it never honours. The placer's
only non-instance obstacle today is mounting-hole circles
(`optimize.py::_hole_keepout_radius_mm`); it has no notion that a corridor
between instances must carry N strands, so nothing in its cost function
resists closing one to 0.093 mm.

## ⚠ The motivating evidence traces to the sink-under-array root cause (2026-09-29)

The `gap 0.093 mm between instances (0, 1, 7)` above is **not an
independent finding**. Measured on the `ewod-dogfood-1` fixture:

- IR indices `(0, 1, 7)` resolve to `ARR1`, `ARR1_SINK_0`, `U_TEMP`.
- The ARR1↔ARR1_SINK_0 centre separation is **0.0932 mm** — the same
  0.093 to three decimals.

So the corridor is 0.093 mm wide because `ARR1_SINK_0` is placed on top of
the electrode array, which `pcb-placement-must-be-valid-before-routing`
already calls invalid and
`pcb-placer-obstacle-set-is-mounting-holes-only` explains: the sink is
born 25.46 mm inside the separation `_placement_is_legal` requires, so the
placer cannot move it and the gap can never open.

**This does NOT close this item.** Two things survive independently:

1. The general claim — that the placer has no representation of "this
   corridor must carry N strands" — is still true, and still unfixed by
   the root-cause work. A legal placement can still starve a corridor.
2. The reporting claim — that a gap admitting 0 strands should surface at
   placement time rather than as four per-net routing failures — is
   likewise untouched.

**What it DOES invalidate is this item's regression fixture.** The
acceptance criteria name "the EWOD dogfood's 0.093 mm / 55-net / 16.5 mm
case" as the case to assert on. That case is a placement-validity bug
wearing a corridor costume; pinning a corridor-capacity term to those
numbers would pin it to a board that should never have existed. This item
needs a fixture whose placement is LEGAL and whose corridor is still too
narrow — otherwise fixing the root cause silently guts the test.

**The root cause landed 2026-09-29**, so this is no longer a prediction:
`_placement_is_legal` and the graded term are side-aware, and the sink
now moves 10.05 mm off the array where it moved 0.0 mm before. Whatever
the 0.093 mm gap becomes, it is not the number above, and the acceptance
criteria below must be rewritten against a fixture built for them before
this item is picked up.

Same shape the campaign keeps finding, one level up: three items
(`...via-geometry...`'s 14 errors, this corridor gap, and the placement
item itself) all took their evidence from one invalid board.

## In scope

1. **Make required corridor width a placement cost.** Given the nets that
   must pass between two instances and the escape class pitch, the
   placer should see the needed width (here 16.5 mm) as a term, not
   discover it after routing. This is the natural companion to item 4's
   polygon keep-out primitive — same missing capability (the placer
   cannot represent a region it must keep clear), different producer.
2. **Report it at placement time.** A gap admitting 0 strands where 55
   nets are assigned is knowable before the maze runs. Surfacing it as a
   placement finding naming the three instances and the needed width is
   strictly more actionable than four downstream per-net failures.

## Explicitly NOT in scope

- The pin-swap / crossing objective (item 3 of
  `pcb-escape-and-driver-chain.md`). Independent lever, and the
  measurement above shows it addresses a different 51 crossings.
- Widening the gap by editing the dogfood fixture. That would hide the
  finding, and the fixture's geometry is design data — the governing
  constraint for this campaign is that the code must not decide it.
- Changing the `gap-capacity` message. It is already precise and it is
  what made this diagnosable; this item is about saying it EARLIER and
  from the pass that can act on it.

## Acceptance criteria

- A board whose placement leaves a corridor too narrow for its assigned
  strand count surfaces that at placement time, naming the instances and
  the required width.
- The EWOD dogfood's 0.093 mm / 55-net / 16.5 mm case is the regression
  fixture, asserted on the numbers above rather than on a yield.
- Escape yield is NOT an acceptance criterion here. Whether opening the
  corridor raises 50/54 is a separate measurement — claiming it in
  advance is what item 3's redirection warns against.

## Target + blast radius

`src/precis/pcb/optimize.py` and `cost.py` (the placement cost terms),
plus wherever the `gap-capacity` finding is produced so the same
computation can be reused rather than reimplemented. Touches placement
for every board, so it needs the fixture caveat from
`pcb-fixture-footprint-manufactures-the-wall`: a yield measured on the
synthetic fixture does not transfer.

## Open questions / decisions log

- **OPEN — refuse, or cost?** A hard refusal at placement time is
  legible but blocks a board the user may intend to route by hand. A cost
  term degrades gracefully but can still produce the 0.093 mm gap under
  enough competing pressure. Leaning: cost term plus a loud placement
  finding, and never a silent pass.
- **OPEN — where does "55 nets want through this gap" come from?** The
  finding already computes it. If that computation lives downstream of
  placement it may need lifting, and that refactor is the real cost of
  this item, not the cost term itself.

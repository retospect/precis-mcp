---
status: draft
title: Flat-pack living hinges — kerf-cut patterns (many staggered parallel cuts) that let a sheet part bend round a corner
pillar: 3d-design
prio: low
blocked-by: flatpack-furniture-generator
---

# Flat-pack living hinges — kerf-cut bend zones

Reto, 2026-10-03, on the flat-pack generator: "many-cut-living hinges
could also be there for the backlog". His reference is stored in the
corpus as `web:rs-online-com-designspark-laser-cut-living-hinges-for-neater`
(DesignSpark, "Laser cut living hinges for neater designs"). This item
follows `flatpack-furniture-generator.md`, which lists living hinges as
out of scope for its v1.

## Motivation / why

A living hinge turns one panel into a curved wall. Laid out flat, the
panel is a field of short parallel cuts that bend round a radius, so a
rounded box needs no extra parts, no glue and no heat bending. In the
generator's terms it is a third joint kind beside finger joints and the
straight option: a bend zone inside a panel instead of a joint between
two panels.

Points from the reference:
- **Pattern.** The pattern is rows of parallel cuts staggered like
  brickwork. It has two row types: a full-length cut with uncut tabs at
  both ends, and a row with the tab in the middle and cuts running out to
  both ends.
- **Spacing.** Row spacing trades flexibility against strength and sets
  the tightest inner bend radius.
- **Cuts, not slots.** Single-line cuts bend tighter than cut-out
  rectangles.
- **Spacing too tight.** At 1 mm spacing, 3 mm acrylic warped. MDF and
  acrylic up to 5 mm both hinged.
- **Test first.** Cut coupons at several spacings and bend each round a
  template whose four corners have different radii, then pick the
  pattern.
- **Assembly.** A hinged wall needs a large corner radius. It locates into
  the base with tabs in slots (6 × 3 mm in the reference) and is
  stiffened by being held between a top and a bottom layer.

## In scope

1. **A bend-zone option on a panel edge in the 2-D panel model.** Its
   parameters are the bend angle, the inner radius `R`, the cut length,
   the row spacing `s`, and the bridge (uncut tab) length.
   - The zone's flat length is the arc length at the neutral layer,
     `(R + t/2)·θ`, with the neutral layer at `t/2` (stated as an
     assumption until a coupon measures it).
   - The cut rows alternate the two row types, so the bridges stagger.
2. **A coupon generator:** one cut sheet of strips at a sweep of row
   spacings, plus the four-radius bend template. The physical test
   records which spacing reaches which radius for a given material and
   `t`. The result becomes a house figure per material, the same way
   `fit` is recorded for finger joints.
3. **Cut file export** through the generator's SVG/DXF path, with the
   hinge cuts on the through-cut layer.
4. **se side:** the 3-D solid shows the bent wall as a swept arc. The
   bend zone is a solid of the same thickness, with a note that its
   stiffness is not modelled.
5. **Checks:**
   - refuse an `R` smaller than the material's recorded minimum (null
     until the coupon), and say so;
   - refuse a cut that runs to within one bridge length of a panel edge;
   - require tabs-in-slots along the edges of a hinged wall.

## Explicitly NOT in scope

- Stiffness, fatigue or cycle-life modelling of the hinge. The reference
  calls it of limited durability, and the coupon test is the only check.
- Patterns other than staggered straight cuts (lattice, spiral, wave).
- Bends about any axis but the one perpendicular to the cut rows.
- Hinges in plywood thicker than the coupon tested. Thick ply wants
  a much longer zone; take it up when a coupon exists.

## Acceptance criteria

- A 5 × 5 × 5 box (units per review-queue item se-machine-design-5) with
  one rounded vertical corner exports a cut sheet. The wall is one panel
  with a bend zone of flat length `(R + t/2)·π/2`, and the staggered rows
  have bridges alternating between rows.
- The coupon sheet for 3 mm corrugated cardboard cuts in one job: 5
  strips at spacings from 1.5 to 5 mm, plus the radius template.
  Recording the passing spacing per radius sets the material's minimum
  `R`.
- Setting `R` below the recorded minimum is refused, naming the figure
  and its source.
- The physical check: the rounded-corner box assembles with the bent wall
  seated in the base slots, and the spacing that worked is recorded in the
  generator's docstring.

## Target + blast radius

The flat-pack generator module (new in `flatpack-furniture-generator.md`)
gains a bend-zone panel feature and a coupon generator. The export gets
no new layer. The checks join the generator's own check family.
`precis-se-flatpack-help` gets a section. No migrations, no worker
changes.

## Open questions / decisions log

- **Corrugated board.** Kerf-cut hinges were shown in acrylic and MDF.
  Corrugated board may bend better with crease lines (half-depth or
  perforated cuts) than with through-cuts, and it bends differently
  along and across the flutes. The first coupon should cut both patterns
  in both flute directions.
- **Neutral layer.** `t/2` is a textbook default. For a kerf-cut zone the
  real neutral layer sits elsewhere, and the coupon should measure the
  flat length that closes the corner.

---
status: draft
title: hexfold sp³ isolation band — a hydrogenated (or fluorinated) belt inside a graphene sheet, for electronic isolation and QM-region cuts
prio: normal
model: opus
blocked-by: hexfold-integration
---

# hexfold: sp³ isolation band

Reto, 2026-09-24: "I also want to provide isolated rings, by having
graphane / hydrogenated C sp³ parts, 2 rows, inside the graphene. For
sim isolation." And on the width: "we can have one hydrogenated step up
and one back down. (also fluorinated)".

**Independent of the smooth layer and of `hexfold-sp3-seam`.** That item
joins three or four *sheets* at an atom; this one decorates sites within
a single sheet. It can land first and does not wait on it.

## Motivation / why

`docs/backlog/rotary-ratchet-valve.md` §"Electronic isolation" already
rules the physics: "A closed loop of sp³ carbons (hydrogenated or
fluorinated) stops conjugation, making the enclosed patch its own
electronic island; that loop is also the right quantum-region cut line,
since terminating at saturated bonds does not sever a conjugated
system." Recorded as a decision in `src/hexfold/spec.md` §30. Nothing
implements it.

The immediate consumer is simulation: a QM region needs a cut line that
does not slice a conjugated system, and an sp³ belt is that line.

## Why this is the cheap sp³ item

Hydrogenating a band removes no rings and mints no defects. Σ(6−n)Pₙ + B
= 6χ is untouched, `counting_residual` stays 0, no curvature charge, no
seam. It is decoration on existing sites — consistent with the §30
ruling that an sp³ patch is "a fitted component with an interface
contract, not a tile", so the ring taxonomy never has to describe it.

Most of the atom-level machinery is already there: `build.py` derives
`hyb="sp3"` when an attachment gives a carbon a fourth bond (the sp3
relabelling near `:3288`), `check.py` raises that atom's valence limit
to 4 (`:54`), `lattice.py` gives it `SP3_IDEAL_DEG = 109.47` (`:21`),
and `stick.py` relaxes against that angle. hexfold atoms already carry
an `element` field, so H and F can live in the net. What is missing is
any way to *ask* for it — sp³ today is only ever a side effect of an
attachment.

## In scope

- A band primitive: **all sites within graph distance d of a closed
  lattice path**. Defining it by distance-from-a-path rather than by
  "rows" is what lets the belt close at any orientation — "row" is
  ambiguous on a hex lattice (zigzag vs armchair) and a loop around an
  island cannot stay in one orientation.
- **Pucker closure sets the default width.** Reto's argument: one
  hydrogenated row steps the sheet up, the next steps it back down, so
  the sheet resumes its original plane on the far side. This is graphane
  chair alternation (H alternating on the two sublattices) read as a
  geometric closure condition, and it is why the natural minimum width
  is two rows rather than one — a single row would leave a permanent
  offset. `d=1` gives that two-atom belt.
- **Terminator is a parameter**, `H | F`. C–H ≈ 1.10 Å, C–F ≈ 1.36 Å.
  Fluorographene is the more strongly isolating and the more strained;
  it also matters for the valve's charge patterning, where the
  electronegativity is the point.
- Relabel banded sites `hyb="sp3"`, add one terminator per site on the
  assigned face, extend `check` to the new state.
- A warning code for the lattice mismatch (below). Not an error.

## Explicitly NOT in scope

- The sp³ *seam* (three or four sheets meeting at an atom) —
  `hexfold-sp3-seam.md` owns that, and it is a genuinely harder
  geometric problem.
- Any electronic-structure claim. This produces a geometry with a
  declared cut line; it does not compute a band gap or a transmission.
- Choosing the QM region. The band is the boundary; region selection is
  the caller's.

## Open questions / decisions log

1. **The two purposes want different widths, and we should not conflate
   them.** A QM cut line needs exactly one saturated ring — cutting at
   sp³ C–C is the whole point, and one row does it. Blocking tunnelling
   is a width question and two rows (~2.5 Å) is thin. Resolve from the
   graphane-nanoroad literature and cite it (cite-sources rule); do not
   assert a number here. The primitive should take a width, with the
   pucker-closure argument setting the default.
2. **Face assignment on a curved sheet.** Chair alternation is the
   default; on high curvature the convex face is sterically preferred,
   which competes with alternation. Decide whether curvature overrides
   the sublattice rule.
3. **Strain is real.** sp³ C–C ≈ 1.53 Å against graphene's 1.42 — about
   7% mismatch, and a *closed* band puts the enclosed island in a stress
   state that may buckle. `stick` will show the pucker but it is a
   preview, not physics. Emit `geom.sp3.mismatch` (WARN, alongside the
   existing `geom.angle.dev`) rather than passing silently.
4. Syntax. Straw man, following the decoration grammar at `text.py:137`:
   `hydrogenate path=(0,0,A)..(6,0,A)..(6,6,A)..(0,6,A) width=1 face=chair`
   with `fluorinate` as the sibling verb, or one verb taking
   `terminator=H|F`. Prefer the latter unless the emitter reads worse.

## Acceptance criteria

- A closed band on a flat sheet and a closed band on a curved sheet each
  `check` clean, with `counting_residual` unchanged from the undecorated
  sheet (the band mints no topological charge).
- Every banded carbon reports 4 bonds and `hyb="sp3"`; every one carries
  exactly one terminator; terminator faces alternate per the chosen
  rule.
- The enclosed island's sp² atoms have no bond path to the outside sp²
  region that does not pass through an sp³ carbon — the isolation
  property stated as a graph assertion, which is checkable without any
  electronic-structure claim.
- `geom.sp3.mismatch` fires on a closed band and not on an open arc.

## Target + blast radius

`src/hexfold/`: `text.py` (grammar), `build.py` (site selection +
relabelling + terminator atoms), `check.py` (new state + warning code),
`lattice.py` (C–F length). `src/hexfold/spec.md` §11 and §30. The precis
side needs nothing new — `hexfold_spec.py` already passes `element` and
`hybridizations` through to `GeneratedBlock`, though its sp²-only bond
order convention (`_SP2_BOND_ORDER`) must not be applied to C–H/C–F.

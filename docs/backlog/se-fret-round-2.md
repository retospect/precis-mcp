---
status: draft
title: se optical domain round 2 — placement solver, DRC fold-in, the regimes round 1 declines to model
prio: normal
model: opus
---

# se optical domain round 2

Round 1 shipped with the FRET leg (`precis_se.fret`, migration
`0010_se_fret.sql`, ops `set_chromophore`/`set_optical_link`/`set_optics`,
`view='fret'`) — the package docstring's **optical domain** paragraph is
the map. This file is only what round 1 deliberately left out, so that
the boundary is written down once instead of being rediscovered by
whoever next opens `fret.py`.

## Motivation / why

Round 1 makes an optical link *checkable*: state a chromophore card on
two blocks, declare a required efficiency, and the L4 view tells you
whether the realized geometry delivers it. What it cannot do is make one
*true*. Every gap below is a place where the view currently reports a
problem an agent then has to solve by hand, or declines to quote a number
at all.

## In scope

1. **The placement solver.** `fret.separation_for_efficiency` already
   inverts `E(r)`, so a declared `min_efficiency` is a distance
   constraint the geometry layer could solve against — the same shape
   `formfind` already has for the axial subgraph (solve poses, write
   back stamped `origin: 'proposed'`, never overwrite a user pose). The
   hard half is that the objective is **two-variable**: moving a block
   changes `κ²` as well as `r`, and the orientation term can undo a
   distance gain entirely. A naive distance-only solver reproduces
   exactly the dead-link failure `fret.kappa_squared` exists to prevent,
   so this needs the joint objective from the start.
2. **DRC fold-in.** Round 1's findings live only in `view='fret'`.
   The house posture is that a declared invariant the realized design
   violates is a `view='drc'` finding — follow `fasten`'s precedent
   (its own view, findings folded into DRC). Rules worth naming:
   `optical_orientation_null`, `optical_below_declared`,
   `optical_unintended_crosstalk`, `optical_out_of_model` (the Dexter
   case).
3. **Per-link medium override.** `set_optics` is design-level today,
   which is right for the common case and wrong for a design that spans
   a membrane or a solvent boundary. The override belongs on the
   `optical` dict, with the design-level value as the fallback — but
   only once a real design needs it, because a per-link `n` that nobody
   sets is a field the view must explain on every line.
4. **Derive the dipole from the bound structure.** In atomic mode a
   block binds a real `structure` design, so its transition dipole is in
   principle *computable* rather than declared — which would close the
   gap where a hand-entered dipole and the atoms underneath it disagree.
   Needs a TD-DFT or semi-empirical route and the per-number provenance
   sidecar (`precis.design.provenance`) to record which one produced it;
   a computed dipole and a datasheet dipole are different fidelities and
   must not read alike.
5. **Expand array members.** An array node is one row in `tree.blocks`
   with one pose; its members are derived at read time. Round 1's view
   therefore counts an array of emitters **once**, at the array node's
   own pose, and says so in a header warning — but an undercounted
   emitter set also undercounts the crosstalk between members, which for
   a linear array at typical pitches is the dominant term. Expanding
   members means giving each one a world pose the view can read, which
   is the same gap `view='clearance'` has; fix both together or neither,
   so se keeps one answer to "what is world space".
6. **Relay chains.** Multi-hop FRET (donor → relay → acceptor) composes
   rates, not efficiencies, and each hop loses. Round 1 solves one donor
   against its acceptors; a chain needs the network solved jointly, and
   a useful chain report is end-to-end throughput plus the identity of
   the worst hop.

## Explicitly NOT in scope

- **A quantitative Dexter model.** Round 1 classifies sub-1 nm pairs as
  `Regime.DEXTER` and refuses to quote a Förster number, which is the
  honest answer. Doing better means transition-density cubes and orbital
  overlap — a different physics stack entirely, not a refinement of this
  one. The regime flag is the deliverable; the number is not.
- **NSET / metal quenching.** Near a metal, transfer goes as `r⁻⁴` and
  the metal quenches besides. se knows a block's material, so *flagging*
  a metallic block near a chromophore is cheap and belongs in the DRC
  fold-in above. Modelling the changed exponent does not.
- **Photobleaching, blinking, symbol-rate budgets.** The comm-system
  layer above the link budget. `fret.Chromophore.lifetime_s` already
  bounds the symbol rate (nanosecond lifetimes ⇒ sub-GHz at best), which
  is enough to stop someone speccing a bus; the stochastic readout model
  is its own item when there is a real protocol to check.
- **Reciprocal space / exciton band structure.** A periodic chromophore
  array coupled strongly enough to delocalize is the *opposite* limit
  from Förster hopping, and Brillouin zones only exist for a lattice.
  If that regime is ever wanted it is a new kind's problem — the
  `structure` enclave already carries lattice and PBC — not an
  extension of this one. Named here because it is the question that
  started round 1, and the answer is "no", which is worth keeping
  written down.

## Acceptance criteria

- A design with a declared `min_efficiency` and free (non-`user`-origin)
  poses can be solved to meet it, or told why it cannot, without a human
  computing a separation.
- The solver never reports success on a geometry whose `κ²` is inside
  the orientation-null band — pinned by a test whose seed geometry is
  distance-perfect and orientation-dead.
- Every `view='fret'` finding has a `view='drc'` counterpart, so a
  caller running only DRC cannot miss a violated optical invariant.
- A relay chain of three blocks reports end-to-end throughput and names
  its worst hop.

## Target + blast radius

`precis_se.fret` (new solver module beside it, not inside it — `fret.py`
stays pure physics), `precis_se.drc`, `precis_se.handler`
(`view='fret'`), `precis_se.ops` (the solver op), `precis_se.atomic` for
item 4, and the `precis-se-help` skill's op/view rosters.

## Open questions / decisions log

- **Decided (round 1):** the chromophore card is block-owned
  (`se_blocks.chromophore`), not its own table — one per block,
  meaningless without it, dies with it, exactly like `dof`.
- **Decided (round 1):** `optical` does not exclude `joint`/`kind` on the
  same connect. Different physics on the same pair, not a competing
  claim about one.
- **Decided (round 1):** the medium index is design-level. Revisit under
  item 3 only when a design actually spans two media.
- **Open:** whether the placement solver is a new op or an extension of
  `formfind`. `formfind` owns "solve poses against declared structural
  invariants" and the optical objective is the same shape — but its
  force-density solver has nothing to say about `κ²`, so sharing the op
  name may promise more than it delivers.

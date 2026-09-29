---
status: ready
title: `fold_layout` refuses bulges, one-sided internal loops and unpaired tails
prio: normal
model: opus
---

# `fold_layout` covers hairpins and multiloops, and refuses much of the rest

## Motivation / why

`fold_layout` (`src/precis_se/chain/fold.py`)
turns a ViennaRNA MFE dot-bracket into helix/strand/domain records. It covers
every pseudoknot-free fold whose consecutive domains are separated by **≥ 1**
unpaired nucleotide — a hairpin, a multiloop, any nesting of them. It refuses,
loudly and by design:

- **zero unpaired nucleotides between two domains** — a bulge, a one-sided
  internal loop, or a coaxial stack;
- **an unpaired 5' or 3' tail**;
- a pair-free fold, a pseudoknotted dot-bracket, > 10 000 nt.

The first two are common in real folds, so the op refuses more often than its
coverage statement suggests. Both refusals are honest (the alternative is
writing a layout that claims something the fold does not say) but they are
placement gaps, not modelling gaps — the decomposition already holds these
shapes.

## In scope

- **Coaxial placement.** The refusal exists because this pass lays every
  helix out one helix-spacing *beside* the previous one, where a 0-nt loop
  between two domains would read as a crossover between neighbouring helices
  — which is not what a bulge is. The fix is to place the next helix on the
  previous one's **end** (collinear, continuing the axis) rather than beside
  it, which is what a coaxial stack physically is. A bulge then becomes a
  short axial offset plus the bulged nucleotide(s) as an unpaired bubble, and
  no loop record is needed between the two domains.
- **Unpaired tails.** A loop today exists only *between* two domains, so a
  dangling 5'/3' stretch has no record. Either give a strand an explicit
  tail field, or represent a tail as a single-occupancy domain on a stub
  helix (the item's own "foothold/toehold = single-occupied domain" reading
  already admits this shape) — decide which, because a realizer will need to
  find it.
- Whichever is chosen, the covered/refused list in `fold.py`'s docstring and
  the `fold_layout` section of `precis-se-chain-help.md` must move with it:
  the current text is accurate and would become a lie.

## Explicitly NOT in scope

- Pseudoknots (needs a second pairing dimension the dot-bracket cannot carry).
- The > 10 000 nt bound, or the separate ≤ 200 nt bound on the *fold checks*
  (`RNA.fold` is O(n³) — that bound is deliberate and lives in the parent
  item).
- Changing the decomposition. Helix-carries-geometry / strand-routes /
  loop-between-domains all survive this; only placement changes.

## Acceptance criteria

- `GGGGAAAACCCC` still yields the parent item's hairpin (1 helix, 4 bp, 2
  domains, 4-nt loop) — this is a strict extension.
- A fold with a single-nucleotide bulge lays out without refusal, its two
  helices collinear rather than side by side, and `view='drc'` reports no
  `chain_loop_short` (the bulge is not a crossover).
- A fold with a 3-nt 5' tail lays out, and the tail is findable from the
  stored records alone.
- Every shape the current pass refuses either lays out or is refused with a
  message naming which of the two lifts above it needs.

## Target + blast radius

`src/precis_se/chain/fold.py` (the entry-building half; `pair_table`/`stacks`
need no change), `precis_se/chain/layout.py` if collinear placement wants a
helper, the skill's `fold_layout` section, `tests/test_se_chain_fold.py`. No
migration. No new dependency — ViennaRNA already ships the fold.

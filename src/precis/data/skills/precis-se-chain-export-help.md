---
id: precis-se-chain-export-help
title: precis — exporting a nucleic-acid chain design from se (scadnano, caDNAno, oxDNA, PDB, order form)
summary: view='export' writes a chain design one way (no import) as scadnano JSON, caDNAno legacy JSON, an oxDNA topology + configuration, a PDB of the realized regions, or the oligo order form (CSV Name,Sequence, one row per strand 5'→3', refusing unsequenced, N-holding or wrong-length strands by name)
answers:
  - how do I export a chain design to scadnano/caDNAno/oxDNA/PDB (view='export')?
  - how do I get the list of strands to order, with their sequences (format='order')?
  - why does the order export refuse my design?
  - why don't the exported helix indices match my caDNAno file?
applies-to: get(kind='se', view='export')
status: active
tags: verbs, design
kinds: se
---

# precis-se-chain-export-help — chain export and the order form

Declaring helices, strands and domains, and filling staple sequences
(`fill_complement`), are in [[precis-se-chain-help]]; atoms for a region
(the PDB format's input) are in [[precis-se-chain-atoms-help]].

`view='export'`, `args={'format': 'scadnano'|'cadnano'|'oxdna'|'pdb'|'order'}` —
the only accepted arg (`precis_se.chain.export`). One direction, no
import.

- **scadnano** — helix index = declaration order; `grid`/`grid_position`
  from the lattice, or `grid: none` + a position in nm off a free path. A
  loop with `n > 0` nt becomes a scadnano *loopout*; the longest strand is
  flagged `is_scaffold`.
- **cadnano** (legacy c2) — lattice designs on **one** lattice only; a
  waypoint-path helix is `Unsupported` naming it. Every vstrand is padded
  to the lattice repeat (32 bp square, 21 honeycomb); a loop's nucleotides
  become a caDNAno insertion (`loop[offset] = n`) at the exit base;
  longest strand → `scaf`, every other strand → `stap`.
- **oxdna** — one `## <design>.top` section then `## <design>.conf`,
  nucleotides per strand listed 3'→5'. An unsequenced nucleotide writes
  `N` (oxDNA needs a real base — this is a geometry export, not a
  runnable input). Loop positions follow the placed curve when
  `relax_chain` wrote one, else the chord between the two exits;
  positions in oxDNA length units (0.8518 nm). A starting configuration,
  not an equilibrium one.
- **pdb** — every `realize_chain`-bound segment, world-posed, one chain
  id per segment, `TER` between. `Unsupported` when nothing is realized
  yet. `get(kind='structure', id=<slug>, view='pdb')` on one minted
  structure writes just that region, with residue names.

- **order** — the oligo order form: CSV `Name,Sequence`, one row per
  routed strand in name order, each sequence 5'→3', named
  `<design>-<strand>` so two designs on one plate don't collide. Paste or
  upload it to the vendor's bulk-order form. Every strand is listed,
  scaffold included — drop its row when the scaffold is bought (M13), not
  synthesised. Refuses, naming every strand it cannot order: one that is
  unsequenced (run `fill_complement`), one holding `N`
  (`fill_complement(unknown='N')`'s placeholder, not a base to order), or
  one whose sequence length differs from its route
  (`chain_sequence_length`). It does not judge pairing — read
  `view='drc'` for `chain_pairing_mismatch` before ordering.

**Helix indices/row/col are ours, not caDNAno's.** The register walk here
is reflected relative to caDNAno's own (a handedness convention) and
unsettled until compared against a real caDNAno file — don't claim
column-for-column agreement.

## See also

- [[precis-se-chain-help]] — the chain ops, `fill_complement`, view='chain'
  and the chain_* DRC findings.
- [[precis-se-chain-atoms-help]] — `realize_chain`, which the PDB format
  writes out.

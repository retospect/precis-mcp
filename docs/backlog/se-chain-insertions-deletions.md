---
status: idea
pillar: 3d-design
---

# se chain insertions/deletions — atoms for a skipped or looped base

IDEA. Insertions and deletions are accepted on a helix's `register`, and
the real-twist account they correct is checked (`chain_twist_global`,
`precis_se/chain/layout.py::twist_residual`); sequences, pairing and the
caDNAno/scadnano exports honour them. What is left is the atom model:
`realize_chain` refuses a region holding an insertion or deletion offset
(`precis_se/atomic/generate.py::prepare_realize_chain`), and the oxDNA
export refuses any insertion. Lab designs sprinkle these through every
long square-lattice sheet (Dietz, Douglas & Shih 2009), so a real design
cannot get atoms for those regions today.

Build: a deleted offset places no nucleotide and its neighbours bond across
the gap (one rise of stretch, strained — say so in the echo); an inserted
base is placed as a bulge off the duplex and relaxed with the loop machinery
(`atomic/generate.py::_chain_loops`), duplex pinned. Residue rows carry the
offset with an insertion index so `view='pick'` can name `stem@12+1`.

Test: realize a region with one deletion and one insertion; the deleted
offset has no residue, O3'–P across it and the inserted base's steps relax
under the loop bound, envelope_fit skips the inserted base like a loop
nucleotide, and pick on the inserted base's atom names it.

Design note kept from slice 1: frames stay at the lattice twist (caDNAno's
convention — units are lattice positions), so insertions/deletions change
the twist account and the sequence, never the crossover offsets.

# se chain insertions/deletions — the global twist correction real origami needs

IDEA (blocked-by: se-nucleic-acid). `se-nucleic-acid` slice 1 stores a
`register.insertions`/`deletions` hook and **refuses it when non-empty**
(`precis_se/chain/vocab.py::vet_register`), because no consumer applies the
correction and silently storing one would read as checked. The 2026-09-29
dogfood named this the first blocker for a design you would actually order:
a square-lattice sheet is modelled at 32 bp = 3.00 turns (10.67 bp/turn),
while real B-DNA runs 10.5 bp/turn, and lab designs sprinkle single-base
insertions and deletions to absorb the difference and keep the sheet flat
(Dietz, Douglas & Shih 2009). Without them every long lattice design is
either modelled at a fictitious twist or reports register errors it would
not have in the tube.

Build: make `insertions`/`deletions` accepted, thread the per-unit twist
perturbation through `precis_chain.register.phase_after`'s reserved
`per_unit_twist=` hook into `chain/layout.py::helix_geometry`, and add the
global-twist finding the register check currently defers — a sheet whose
accumulated twist over its length exceeds a tolerance, reported against the
helix set rather than one helix. Then `chain_twist_register` is measured
against the corrected phase, not the nominal lattice repeat.

Test: a 6-helix × 64 bp square-lattice sheet at 10.5 bp/turn accumulates a
named twist; the same sheet with one deletion per 48 bp comes back flat
(accumulated twist under tolerance), and the deletions change the
register-correct crossover offsets in the direction the corrected phase
predicts — recomputed from the returned frames, not asserted as a constant.

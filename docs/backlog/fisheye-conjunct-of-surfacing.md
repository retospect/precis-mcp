# fisheye Claims ring doesn't surface conjunct-of edges

`precis.utils.refeye` derives the Claims ring via `derive_refines` only;
`conjunct-of` edges (migration 0126, taproot-compound-migration) are written but
invisible in the one human-facing surface a reviewer uses. Matters before
the planned migration pass over the existing compound hubs — the reviewer
approving splits can't see atom↔compound structure via fisheye. Wire
`seniority.derive_conjuncts` / `conjunct_atoms_bulk` into the Claims ring
(direction: atom→compound; render distinct from refines). Found in pre-ship
review of the atomic-claims build. Owner `src/precis/utils/refeye.py`.
Test: a compound hub's fisheye shows its conjunct atoms and vice versa.

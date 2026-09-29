# se chain staple sequences — scaffold sequence in, orderable strands out

IDEA. Pairing is derived from co-occupancy, so
the tool already knows, for every staple offset, which scaffold base sits
opposite it — but nothing fills the staple's own sequence. The 2026-09-29
dogfood put it plainly: "the thing you literally paste into an order form is
the one thing it won't produce." A designer must currently hand-type every
staple, which is exactly where an off-by-one costs money.

Build: a pure op `fill_complement(strand=…)` (or `fill_complement(design=…)`
for every strand with no sequence) that walks each domain's offsets, reads the
co-occupant's letter via `chain/pairing.py::derive_pairing` +
`strand_letters`, and writes the Watson–Crick complement in 5'→3' order,
respecting `forward` and skipping loop nucleotides (which pair with nothing
and must be supplied, not derived — a loop with no authored letters is an
`Unsupported` naming it, never an invented run of T). DNA vs RNA from the
strand's `nucleic`. Refuses to overwrite an authored sequence without
`overwrite=True`.

Also owed, and the reason this is worth its own item rather than a one-liner:
**a sequence-length check**. Slice 1 accepts a 17-nt sequence on a 9-nt route
with no finding (dogfood, severity friction) — `view='chain'` prints
`route_nt 9 / sequence 17 nt` side by side and DRC stays silent. Add
`chain_sequence_length` (error) comparing a strand's sequence length against
its route length plus its loop nucleotides, and it lands here with the fill so
the fill's own output is checked by the same rule.

Not in scope: sequence *design* — no melting-temperature optimisation, no
repeat/GC screening, no scaffold selection. Complement fill only; the
thermodynamics belong with a real strand-design tool.

Test: a 4-helix tile whose scaffold carries a sequence and whose five staples
carry none → `fill_complement` fills all five, every filled base is the
Watson–Crick complement of the derived co-occupant recomputed from the tree,
and the round trip through `view='chain'` shows `route_nt == sequence` for
every strand; a staple with a 4-nt loop and no authored loop letters raises
`Unsupported` naming the loop; a 17-nt sequence on a 9-nt route fires
`chain_sequence_length`.

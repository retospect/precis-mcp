# Taproot — gate evidence to the paper's own results, not its lit review

A claim hub accepted a paragraph from a prior-work/review section as evidence.
That paragraph's own sources did not support the claim, so the chase laundered
an unchecked assertion into a "verified" one — a false-verification path, not a
ranking nuisance. Evidence must come from the meat of a paper ("X was doped
with Y and we measured Z"), never from "it is known that…": a review paragraph
cites *elsewhere*, so attaching one asserts a provenance the chunk doesn't have.

Fix: gate admissible evidence on the `role3` axis the chunk classifier already
defines (`own` / `background` / `furniture`, `src/precis/workers/classify.py`)
— accept `own`, refuse `background`, and surface the refusal reason so a chase
can look elsewhere rather than silently scoring lower. Owner
`src/precis/taproot/` (attachment in `hub.py`, scoring in `trust.py` /
`seniority.py`).

Hard dependency: `role3` must actually be populated. The classifier is
default-OFF and the melchior handler has not written a `role3` value since
2026-08-08 (gripe gr204385) — so ship the backfill first, or the gate refuses
everything. Until then a downgrade-not-refuse variant is the safe interim.

Found the hard way during the nanobuds review, 2026-08-13. Correctness;
medium.

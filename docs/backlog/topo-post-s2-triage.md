---
status: draft
---
# Post-S2 capacity and via triage

Before sizing S3/V, classify the retained failures at a pinned S2 commit.
The baseline is `0e19ce0a0`; its saved routing measurements are verified, but
its retained-identity bug remains under correction. Results from this study
do not replace the final corrected S2 gate.

Read all twenty real-rule rows and identify guarded shared-edge failures,
other guards, unreachable nets, missing-via reasons and plane-pad failures.
Use the original `shove_emit.py` shared-edge definition and distinguish
direct track guards from barrel/pad offences. Resolve planned branch indices
through final identities; never index a pre-rethread branch list directly.

Reuse saved evidence where it answers the question. Reproduce only rows
needed to recover missing geometry, with explicit source/import/fixture
provenance and unchanged rule values. S3 is dropped when its specified
post-S2 guarded-shared count is below ten; do not replace that count with
all guarded nets or a guessed attribution.

Prepare the controlled real-web versus zero-web experiment for V. Only the
web changes: trace clearance, via size, pad geometry, labels, order, poses and
all other rules remain fixed. A single-row smoke verifies instrumentation;
a full matrix waits for explicit follow-up after corrected S2 is accepted.
Report missing affordances rather than inferring a failure cause from a name.

This study writes scratch evidence only, under the shared heavy-validation
lock. It does not enable a capacity heuristic, move a via, relax a rule or
change production routing.

## Observed at the pinned pre-round13 source

All twenty real rows were classified; all six guarded replays match. The
guarded-shared count is ten, so the below-ten S3 drop condition is not met.
The saved census has 120 unreachable-net instances, fourteen guarded
instances, thirty-one plane-pad misses and zero missing-via errors. These
are repeated benchmark instances, not counts of unique board nets.

The one-row web-only smoke remains 15/20; that is not a full web A/B result.
After corrected S2 is accepted, isolate unreachable nets under unchanged
geometry/rules to distinguish static access from earlier-copper competition,
then use the controlled web runner where needed. Confirm an actual interval
capacity shortfall in the ten shared-edge cases before implementing S3.
An old count of web failures does not by itself justify a via-resiting patch.

# Preserve synchronized agent history during intake

PCB intake reproduced a `scripts/take-agent` defect: after a normal merge
of the coordinator into the clean agent branch, its unconditional rebase
still flattens older merge commits and replays conflicting intermediate
work. If the coordinator branch is already an ancestor of the agent
branch, no rebase is necessary. Skip that operation, then retain the same
fast-forward merge, liveness/cleanliness checks and intake receipt.

Add a temporary-repository regression with an agent merge history and a
coordinator synchronization merge. It must preserve the exact agent tip
and commit ancestry and still record the intake. Keep the existing
rebase behavior when the agent does not contain the coordinator. No
production deployment or change to shipping authority is involved.

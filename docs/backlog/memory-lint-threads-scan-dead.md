---
status: ready
title: memory-lint check 2 (landed-thread scan) is dead — reports a false "clean"
---

# memory-lint landed-thread scan scans nothing

## Defect

`scripts/memory-lint` check 2 slices the index with
`awk '/^## Threads/{f=1;next} /^## /{f=0} f'`. The script's own header
documents MEMORY.md as grouped into `## Threads` / `## Runbooks` /
`## Gotchas` / `## Workflow` / `## Reference`, but the live
`~/.claude/projects/-Users-reto-precis-mcp/memory/MEMORY.md` has **zero `##`
headings** — one flat list of 80 bullets. The awk slice returns empty, the
`while read` loop body never executes, and the check passes vacuously.

Observed 2026-09-14: the session-start hygiene line reads
`memory-lint: ✓ clean (16063 B, links resolve, no landed threads lingering)`
while the index still carries campaign bullets marked SHIPPED+DEPLOYED. The
"no landed threads lingering" clause is a **false negative**, not a clean
bill of health.

Consequence: the auto-catch that was supposed to drive retirement has never
fired, so landed state accumulates in the one always-loaded file. The
reconsolidation pass has been running without its punch-list.

## Why it drifted

The grouping is hand-maintained (the header says MEMORY.md is HAND-EDITED,
don't generate it) and nothing checks it exists. Structure with no enforcing
check reverts to flat.

## Fix

1. Make the absence of the expected `## ` groups a *finding*, not a silent
   empty scan — if the index has no `## Threads`, say so rather than
   reporting clean.
2. Restore the five groups in MEMORY.md (user-level file, outside the repo —
   not shippable in the same commit).
3. Consider widening the scan to every bullet regardless of section, with
   the section used only for ordering. The landed test itself
   (`git merge-base --is-ancestor` over cited shas + the open-work-words
   regex) is sound; only its input set is broken.

## Test

With a flat index, memory-lint reports the missing-groups finding instead of
`no landed threads lingering`. With the groups restored, a memory file whose
every cited sha is in `main` and which carries no open-work words is
reported as a landed thread.

## Related

`context-hierarchy-dag.md` folds this fix into its P0 — the same oracle is
the recency-band mechanism there. Fix it independently if that item stalls;
the false "clean" is wrong on its own terms.

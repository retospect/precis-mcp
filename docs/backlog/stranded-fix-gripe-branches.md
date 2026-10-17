---
status: draft
pillar: platform
prio: high
---

Status 2026-10-17: only gr451356 is left, blocked on Reto's ruling for where mating direction comes from (feaf147d0).

# Stranded fix_gripe branches — what is left after the 2026-10-01 landing

The fix_gripe lane (now OFF, 2026-10-02) pushed 43 branches into the agent-lane
worker's own checkout instead of the shared remote and reported success
(gr458326, fixed 2026-09-30). All 43 were bundled off the node, test-merged against main and
reviewed on 2026-10-01; Reto: "land them". **Done:** 18 keepers landed on
main as one squash, and `gripe_182230`'s chase-coverage ledger (rewritten,
migration 0175) went through the gate and is deployed; six gripes closed against the squash, six left open with a
comment naming the unlanded part, the rest were already closed; the 14 drops are
deleted on the node, and the other 29 followed on 2026-10-02 (Reto) — the
node's fix checkout holds only `main`; its 67 scratch clones (18G) are
removed. The bundle of all 43 (`~/work/archive/stranded-gripes-2026-10-01.bundle`
on Reto's Mac) is the only copy now and the source for every branch below.
Local-branch sweep 2026-10-15: nothing deletable — the only sessionless
unprotected branches (`allow-host-ops`, `spso-picker-spec`) both carry
unmerged commits; every merged branch is still held by a live worktree.
Details and remote-only stragglers: /tmp/gripe-fleet/morning-queue.md
"stranded branches".

## Left

1. **Salvage (1 left)** — `gripe_451356` (ewod-pcb / pcb-easyeda-round-trip:
   substring package match false-flags; where mating direction comes from is
   undecided — waits on that ruling). The other seven were redone by hand on
   main and their gripes closed: gr451276 (43448c0b3, pads as router
   obstacles of their own net), gr453859 (8379c61ae), gr453860 (93d8b431a,
   428f9b14c — the heal that shipped is the bounded one, not the ~914-ref
   fan-out), gr458084 (46d66f11b, ca868ca55), gr248866 (per-process
   attestation, verified 2026-10-03), gr452203, gr454480. Audit 2026-10-17:
   the bundle was not found at that path on Reto's Mac.
2. **Optional (2)** — `gripe_180306` (hub reconcile sweep: a feature, one LLM
   call per candidate pair; the reported pair is already merged; a cheaper
   design is waiting on Reto under knowledge-mesh), `gripe_451269` (one docs
   paragraph).
Residual filed while landing: **gr460408** (pathway barriers come back NaN;
only `barriers_ranked` handles it).

---
status: ready
pillar: platform
prio: high
---

# Stranded fix_gripe branches — what is left after the 2026-10-01 landing

The auto-fix lane pushed 43 branches into the agent-lane worker's own checkout
instead of the shared remote and reported success (gr458326, fixed
2026-09-30). All 43 were bundled off the node, test-merged against main and
reviewed on 2026-10-01; Reto: "land them". **Done:** 18 keepers landed on
main as one squash, and `gripe_182230`'s chase-coverage ledger (rewritten,
migration 0175) went through the gate and is deployed; six gripes closed against the squash, six left open with a
comment naming the unlanded part, the rest were already closed; the 14 drops are
deleted on the node, and the other 29 followed on 2026-10-02 (Reto) — the
node's fix checkout holds only `main`; its 67 scratch clones (18G) are
removed. The bundle of all 43 (`~/work/archive/stranded-gripes-2026-10-01.bundle`
on Reto's Mac) is the only copy now and the source for every branch below.

## Left

1. **Salvage (8)** — lift the idea, redo by hand, in the owning thread:
   - ewod-pcb / pcb-easyeda-round-trip: `gripe_451276` (the router still
     treats a pin's second pad as a foreign obstacle), `gripe_451356`
     (substring package match false-flags; where mating direction comes from
     is undecided).
   - ingest-and-fetch: `gripe_453859` (lift the re-arm guard and
     `--needs-manual`; share one pass definition with the landed 453862),
     `gripe_453860` (keep the journal and census, drop the auto-heal — it
     fans out ~914 refs on its first pass).
   - se-3d-viewer: `gripe_458084` (counter, prev/next links and banner stay on
     the loaded revision after an in-place step).
   - monitors-that-go-quiet: `gripe_452203` (the dedup still misses the
     gripe's own example), `gripe_454480` (an already-fixed run reopens the
     gripe, so it can loop), `gripe_248866` (unverified whether a child
     process inherits the heartbeat's macOS TCC grant, which would make the
     probe a false green; the interpreter list duplicates deploy's).
2. **Optional (2)** — `gripe_180306` (hub reconcile sweep: a feature, one LLM
   call per candidate pair; the reported pair is already merged; a cheaper
   design is waiting on Reto under knowledge-mesh), `gripe_451269` (one docs
   paragraph).
Residual filed while landing: **gr460408** (pathway barriers come back NaN;
only `barriers_ranked` handles it).

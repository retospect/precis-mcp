# Quest sim-seed orphan recovery + audit-less bulk delete hardening

Two linked gaps from the gr204309 diagnosis (2026-08-12):

1. **Orphaned seed todos don't recover.** q164903's 9 OPEN:ephemeral
   autocatpath seed todos each lost their only `autocatpath_seed` job
   child to a bulk soft-delete (2026-08-11 14:00:58Z); `_pending_sim_ids`
   filters `deleted_at IS NULL`, so backpressure went blind and nothing
   re-mints a child for a seed todo stuck mid-compute-lifecycle. Design a
   re-mint path (quest tick notices a live seed todo with zero live sim
   children → re-mints once) that CANNOT reintroduce the 238-seed runaway
   (see the backpressure comment in `src/precis/quest/tick.py` ~L82) —
   cap per tick, respect `_SIM_JOB_TYPES` backpressure, idem-key per todo
   generation. Also the one-time prod remediation for the 9 todos
   (td201904 202042 202121 202163 202241 202486 202708 202746 202837).

2. **Audit-less bulk soft-delete.** Those 9 job refs were soft-deleted in
   one transaction with ZERO ref_events — something bypassed
   `Store.soft_delete_ref`/`append_event` (raw SQL or untracked tooling,
   actor unknown). Find the writer (grep tooling/crons for bulk
   `UPDATE refs SET deleted_at`), and consider a DB trigger or store-layer
   invariant so kind='job' soft-deletes always leave an event trail.

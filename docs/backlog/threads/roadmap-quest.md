# bootstrap roadmap quest

**Status:** ends when the root roadmap quest ticks unattended, writes rungs
that carry numbers, and drives priority down to the pathway quests with a
reviewed ledger behind every tick. Today stages 1-4 are built, gated and
on the fleet; the prod dry-run has been seen (2026-09-30 13:24 UTC, role
demand on qu453869), the one defect it showed is fixed and deployed (14:12
UTC); the first live tick is the first real verification and waits on
Reto's word (td458387).
**Last reviewed:** 2026-09-30
**Worktree:** `roadmap-quest`

## Do next

1. **qu453863 first live tick** — Reto's explicit word only. The prod
   dry-run ran 2026-09-30 13:24 UTC (deploy session, Reto-approved, exit 0,
   no writes): role `demand`, tier big, gap `no-demand` on
   `placement_error_nm` for qu453869 (no se part serves it yet, so the
   prompt tells the model to derive the number from the capability
   statement alone). The dry-run found one prompt defect, fixed the same
   day: the capability heading was the ledger's 60-character display stub,
   cut mid-sentence before the clause that names the tolerance — the prompt
   now carries the full statement (`LedgerRow.capability_statement`),
   gated and on the fleet since 2026-09-30 14:12 UTC — the build the first
   live tick runs on. The go/no-go is td458387 in Reto's queue. Fail
   signals on the first ticks: a rung minted without a number; deed count
   climbing while no ledger value changed.
2. **qu453863** — activation, Reto-approved write only after 1; serves
   qu161906 so PRIO flows down to the pathway quests (qu453865–qu453878,
   qu330435, qu347422) once it ticks.
3. **backlog/bootstrap-roadmap-quest.md §Residuals 5** — export `rungs_for`
   from the ledger so roadmap_tick stops re-deriving rung status with its
   own SQL; a drift between the two queries is invisible (no finding), so it
   outranks the cosmetic residuals.
4. **backlog/bootstrap-roadmap-quest.md §Residuals 2, 3, 4** — "lowest unmet
   capability" is the builder's reading not a ruling; supply-absent rows
   route to supply not bridge; first-tick deed baseline seeds silently. All
   three become decidable only after 1 shows real ticks.

## Horizon

1. **qu453863 ticking cadence** — waits on three watched ticks (Do next
   1-2); rungs that carry numbers, driving PRIO down through qu161906 to the
   pathway quests.
2. **backlog/bootstrap-roadmap-quest.md §Residuals 2-4 ruled** — waits on
   real ticks showing which capability the root picks; rulings replace the
   builder's readings.
3. **backlog/bootstrap-roadmap-quest.md §Residuals 6** (capped framing chunk
   + web hub ledger panel) — waits on three clean ticks.
4. **backlog/knowledge-mesh.md in-scope 2** (meta.supply widened to
   measures) — waits on measures-substrate (term-taxonomy thread); supply
   numbers with identity instead of free floats.
5. **backlog/quest-dossier-dialectic.md** — waits on the catpath schema;
   estimate-based dialectic ticks for the pathway quests.
6. **backlog/curation-gate.md** — waits on eval-run-spine's verdict column;
   review of what each tick wrote before it feeds the next.

## Parked

- (none beyond Horizon's stated waits)

## No action needed

- Residual 1 (`benign` cannot be stored) — fixed, gated and deployed
  2026-09-30; nothing further.
- Reto's four build rulings — recorded in the decisions log; nothing
  pending.

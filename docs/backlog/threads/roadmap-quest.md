# bootstrap roadmap quest

**Status:** ends when the root roadmap quest ticks unattended, writes rungs
that carry numbers, and drives priority down to the pathway quests with a
reviewed ledger behind every tick. Today stages 1-4 are built, gated and
on the fleet; the first live tick ran 2026-09-30 22:01 UTC on Reto's word
(role demand, tier big): it wrote qu453869's first demand number,
`placement_error_nm = 0.15` with a stated reason, minted no rung and no
deed, and neither fail signal fired. Activation is the next prod write.
**Last reviewed:** 2026-09-30 (pillar review same day added three orphan
gripes to Horizon, all adjacent infra the tick path depends on)
**Worktree:** `roadmap-quest`

## Do next

1. **qu453863 activation** — no longer needs Reto's word
   (docs/conventions/thresholds.md, 2026-10-01: ticks and activation are
   go). The first live
   tick (2026-09-30 22:01 UTC, `scripts/prod-precis quest tick 453863`,
   exit 0) took the `demand` role at tier big on the `no-demand` gap and
   wrote `meta.demand.placement_error_nm = 0.15` on qu453869, reason "one
   covalent bond length", source qu453869 itself (no se part serves it
   yet). No rung, no deed, no ledger improvement — the demand role only
   writes the number; rungs come from `bridge`. Neither fail signal fired
   (a rung without a number; deeds climbing on a flat ledger). Tick 2
   (2026-10-01 ~12:20 UTC, exit 0) changed role to `supply` @big: 3 S2
   searches, 4 papers linked `serves` the quest, 0 hubs, no quantified
   claim, so `meta.supply` stayed unwritten; dry, gaps [2, 2]. It took
   ~25 min because quest S2 search runs keyless and double-retried
   (gr459597, 61×429) — fix that before activation or every supply tick
   stalls. One more watched tick (expect `bridge` or a supply with a
   number), then activation (no ask, `docs/conventions/thresholds.md`). Serves
   qu161906 so PRIO flows down to the pathway quests (qu453865–qu453878,
   qu330435, qu347422) once it ticks unattended.
2. **gr458880 — `view='tree'` on the session MCP raises Internal
   ImportError** (get(kind='quest', id=453863, view='tree'), 2026-09-30
   22:05 UTC); the progress read the thread names is unusable from a
   session until the shared server carries the roadmap build. The CLI
   path is unaffected; `view='raw'` shows the meta.
3. **backlog/bootstrap-roadmap-quest.md §Residuals 5** — export `rungs_for`
   from the ledger so roadmap_tick stops re-deriving rung status with its
   own SQL; a drift between the two queries is invisible (no finding), so it
   outranks the cosmetic residuals.
4. **backlog/bootstrap-roadmap-quest.md §Residuals 2, 3, 4** — "lowest unmet
   capability" is the builder's reading not a ruling; supply-absent rows
   route to supply not bridge; first-tick deed baseline seeds silently. All
   three become decidable only after 1 shows real ticks.

## Horizon

0. **backlog/quest-tick-local-first-search.md** — inserted 2026-10-01 by
   the pillar review on Reto's word ("ticks should look locally first"):
   S2 acquisition fires on every query today and the local leg sees only
   papers. Owner re-ranks; every tick that searches pays for it.
1. **qu453863 ticking cadence** — waits on three watched ticks (Do next
   1-2); rungs that carry numbers, driving PRIO down through qu161906 to the
   pathway quests.
2. **backlog/bootstrap-roadmap-quest.md §Residuals 2-4 ruled** — waits on
   real ticks showing which capability the root picks; rulings replace the
   builder's readings.
3. **backlog/bootstrap-roadmap-quest.md §Residuals 6** (capped framing chunk
   + web hub ledger panel) — waits on three clean ticks.
4. **backlog/knowledge-mesh.md in-scope 2** (meta.supply widened to
   measures) — waits on measures-substrate (knowledge-mesh thread); supply
   numbers with identity instead of free floats.
5. **backlog/quest-dossier-dialectic.md** — waits on the catpath schema;
   estimate-based dialectic ticks for the pathway quests.
6. **backlog/curation-gate.md** — waits on eval-run-spine's verdict column;
   review of what each tick wrote before it feeds the next.
7. **gr345366** — `quest_tick` fails ~4x/48h on qu164903 with "unparseable
   model output"; not yet root-caused (candidates: truncated reply, a
   non-dict router `.data`, a stray leading brace). The same tick path
   qu453863 will run through once it activates.
8. **gr453861** — an executor-bearing todo (the shape qu453863's own tick
   dispatch uses) sits STATUS:open with no child job for up to ~18 minutes
   with no signal distinguishing normal minter cadence from a stalled
   dispatch; worth an observable before the first live tick's silence is
   mistaken for a wedge.
9. **gr454792** — neither documented path actually unparks a
   child-failed-final leaf; if a `roadmap_tick` job ever lands there, the
   two-tag manual recipe in the gripe is the only one that works.

## Parked

- (none beyond Horizon's stated waits)

## No action needed

- Residual 1 (`benign` cannot be stored) — fixed, gated and deployed
  2026-09-30; nothing further.
- Reto's four build rulings — recorded in the decisions log; nothing
  pending.

# bootstrap roadmap quest

**Status:** ends when the root roadmap quest ticks unattended, writes rungs
that carry numbers, and drives priority down to the pathway quests with a
reviewed ledger behind every tick. Today stages 1-4 are built, gated and
on the fleet; the first live tick ran 2026-09-30 22:01 UTC on Reto's word
(role demand, tier big): it wrote qu453869's first demand number,
`placement_error_nm = 0.15` with a stated reason, minted no rung and no
deed, and neither fail signal fired. Four watched ticks ran; qu453863 was
tagged `STATUS:active` 2026-10-02 ~02:00Z (no ask, thresholds.md) and now
ticks unattended. The unattended ticks (04:24–09:57Z 10-02) proved the
embedder fix live but exposed a supply defect: both cited supply numbers
are one misread value (see Do next 1). Next evidence: a supply tick after
the extraction-window fix deploys.
**Last reviewed:** 2026-10-02 (hold lifted; fix + Do-next residual 5 landing)
**Worktree:** `roadmap-quest`

## Do next

1. **Watch the first supply tick after the round deploys the extraction
   fix** (3b032a98d: `_paper_servers` newest-first, plus a prompt clause
   rejecting a method's own measurement uncertainty) and the verbatim-quote
   check (in build 2026-10-02, orchestrator verdict
   `reviews/roadmap-quest.review.md` §2). Root cause, found by the backwards
   run: pa459574 has 0 chunks (stub, `DREAM:acquire`), so its card was its
   title only and the model supplied "an uncertainty margin of 1.2 nm" from
   recall. The check: the quote must be a substring of the card the model
   was shown; number within rounding plus unit; refuse after `±`; word cues
   (uncertainty / error / resolution / precision) are held and tagged
   `review:supply-held`, not refused. Text-less papers are dropped from the
   findings prompt. The repair is DONE
   2026-10-02 ~12:15Z (Reto approved review item roadmap-quest-1):
   `meta.supply = {}`, rungs td460713 + td460923 `STATUS:won't-do`. If a
   tick re-cites pa459574's 1.2 nm before the deploy, clear `meta.supply`
   again after it. After the deploy, read every stored `meta.supply` value
   against its finding's source passage. The tick only writes a supply value
   that BEATS the stored one (and `best_supply` reads `meta.supply` first),
   so a misread value in the better direction (too small on a `min` axis)
   can never be displaced by a tick. The only way to correct it is by hand:
   `edit(kind='quest', id=<cap>, meta={'supply': {...}})`, which replaces
   the whole `supply` dict, so re-send every key you keep (pass `{}` to
   clear it, as on 10-02).
2. **Local-first never yields to S2.** Every unattended supply query logged
   `[local 10, acquired 0; outside skipped, graph answered]`:
   `relevance_floor()` defaults to 0.0 and the semantic leg's
   `SEMANTIC_DISTANCE_FLOOR` (0.65) admits 10 hits for any query in this
   corpus, so `LOCAL_ENOUGH = 3` is never binding and outside search never
   runs. The `drift_per_cycle_nm` supply ticks at 08:36 and 09:57Z came back
   dry this way. ACCEPTED (orchestrator 2026-10-02): wait for one supply
   tick after the deploy; if it is still dry, build "escalate on a dry tick
   for that key". After a supply tick on (capability, key) writes nothing,
   the next supply tick on that key runs the external leg whatever the
   local count. Bound: after two dry ticks with the external leg on, stop
   escalating that key and log it as "not found outside" with the queries
   used, so it shows as a gap and not as spend. Filed, not to be built now:
   count a local hit toward `LOCAL_ENOUGH` only when it carries a
   quantified claim for the key (the better rule; costs a claim read per
   hit).
3. **Watch qu453863 ticks after the fix deploys** — `STATUS:active`
   confirmed 10:38Z 10-02. ecefede3's embedder fix is LIVE (every query
   since 04:24Z logs `local 10`, no lexical-only clause). Check: no rung
   without a number; deeds do not climb on a flat ledger; a supply number
   cites a paper linked by its own tick. If either fail signal fires, tag
   it back to `STATUS:dormant` and file. History: the first live
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
   ~25 min because quest S2 search ran keyless and double-retried
   (gr459597, 61×429); fixed 2026-10-01 together with local-first search
   (S2 only when the graph has fewer than 3 relevant hits). Tick 3
   (2026-10-01 23:15Z, 3.5 min, no 429) took supply @big again and wrote
   qu453869 `meta.supply.placement_error_nm = 1.2` [fi460566], 3 papers
   linked, 1 ledger improvement — but every query logged `local 0`: the
   CLI tick built its search with no embedder, so the local leg was
   lexical-only (fixed 2026-10-02; the worker path was unaffected). Tick 4
   (2026-10-02 ~01:55Z, exit 0) took `bridge` @frontier and minted rung
   td460713 "Port single-atom tip placement to liquid at room
   temperature" (1.2 nm today vs 0.15 nm needed; cites fi460566,
   fi176418; serves qu453867, qu453869; tagged `waiting-for:reto`); 0
   ledger improvements, gaps [2, 1], both fail signals clear. Bridge does
   no search, so local-first is still unproven live. Serves
   qu161906 so PRIO flows down to the pathway quests (qu453865–qu453878,
   qu330435, qu347422) once it ticks unattended.
4. **backlog/bootstrap-roadmap-quest.md §Residuals 2, 3, 4** — "lowest unmet
   capability" is the builder's reading not a ruling; supply-absent rows
   route to supply not bridge; first-tick deed baseline seeds silently. All
   three become decidable only after 1 shows real ticks.

## Horizon

1. **qu453863 ticking cadence** — ticking unattended since 2026-10-02;
   waits on Do next 3's watch; rungs that carry numbers, driving PRIO down
   through qu161906 to the pathway quests.
2. **backlog/bootstrap-roadmap-quest.md §Residuals 2-4 ruled** — waits on
   real ticks showing which capability the root picks; rulings replace the
   builder's readings.
3. **backlog/bootstrap-roadmap-quest.md §Residuals 5** (capped framing chunk
   + web hub ledger panel) — waits on three clean ticks.
4. **backlog/knowledge-mesh.md in-scope 2** (meta.supply widened to
   measures) — waits on measures-substrate (knowledge-mesh thread); supply
   numbers with identity instead of free floats.
5. **backlog/quest-graph-as-dossier.md** — Reto 2026-10-01: the quest's
   graph is the dossier, a writer agent linearises; supersedes
   quest-dossier-dialectic. Ranked in graph-memory-consumers Horizon 1; the
   quest loop's dossier consumers retarget once its relation and
   linearisation questions are decided.
6. **backlog/curation-gate.md** — waits on eval-run-spine's verdict column;
   review of what each tick wrote before it feeds the next.
7. **gr453861** — an executor-bearing todo (the shape qu453863's own tick
   dispatch uses) sits STATUS:open with no child job for up to ~18 minutes
   with no signal distinguishing normal minter cadence from a stalled
   dispatch; worth an observable before the first live tick's silence is
   mistaken for a wedge.
8. **gr454792** — neither documented path actually unparks a
   child-failed-final leaf; if a `roadmap_tick` job ever lands there, the
   two-tag manual recipe in the gripe is the only one that works.

Quest-loop machinery, ranked here 2026-10-01 (engine and qu164903 items are
the chemistry thread's):

9. **backlog/quest-tick-slicing-residuals.md** — requeue-from-checkpoint and
   stale-stage agentlog finalize; the stage machine shipped, these are the
   residual failure paths.
10. **backlog/quest-loop-safety.md** — the "rubric key never produced"
    warning (the anti-spin breaker shipped, gr170252); a silent empty
    frontier is the failure it names.
11. **gr459054** — `quest/roadmap_tick.py` imports `precis_se.handler`, the
    one grandfathered breach of the plugin import boundary (plugin-split
    owns the boundary; the fix is in quest code).
12. **backlog/quest-loop-cadence-strip.md** — the web dashboard shows no
    cadence or why-not-ticking.
13. **backlog/quest-bodies.md** — the `inquiry` body and qu401863's restart
    checklist.
14. **backlog/web-quest-editor.md** — create/reprioritise the quest tree from
    the web; last, a human surface over a loop that must tick first.
15. **backlog/todo-tree-plan.md** — remaining fold candidates of the
    todo-tree plan; the todo tree is the quest loop's work substrate, so it
    sequences after the loop ticks. Platform pass 2026-10-02.

## Parked

- (none beyond Horizon's stated waits)

## No action needed

- Residual 1 (`benign` cannot be stored) — fixed, gated and deployed
  2026-09-30; nothing further.
- Reto's four build rulings — recorded in the decisions log; nothing
  pending.

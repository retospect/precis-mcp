---
status: draft
title: Gripe = intake and triage record; a linked code todo is the work item
pillar: platform
prio: high
---

# Gripe = intake and triage record; a linked code todo is the work item

Source: gr554555 (Reto, 2026-11-02: "please do, this is great"), with
WORK_MANAGEMENT's eight constraints (comment 1 on the gripe). Design pass
only; the build is phased and starts after Reto reads this.

Bottom line. The gripe keeps what a todo must not have: open filing,
dedupe (`meta.dup_parents`), flood control, triage verdicts, severity, the
`resolved:` close gate and the never-close mark. Everything about *doing*
the work moves to one code todo per accepted gripe, linked `todo → gripe`
with the existing `fixes` relation: route, risk, dependencies, the lease,
the brief, pass children and lane attempts. The code todo is a plain
`plan_dispatch` tree (phase 2) under one tactical root; no second
scheduler, no new STATUS value, no new relation, no migration. Nothing in
this design can mint a billed `plan_tick`: every code todo is written with
the documented `meta={'llm_tier': None}` opt-out and never with
`meta.executor`, and the dispatch worker's candidate query needs one of
those signals to see a todo at all. Phase 1 needs no code: the GRIPE_FIX
orchestrator mints the todo at dispatch with four calls.

What exists today and is reused as is: the gripe status ladder
(`triaged → fixing → auto_verify|user_verify → done`,
[gripe-pipeline-v2](gripe-pipeline-v2.md)), the loop skill
([gripe-fix-loop-on-the-gripe](gripe-fix-loop-on-the-gripe.md)), the
plan tree, `run_slice`, `land_slice`, the `gripe-fix` template and the
`gripe_validate` tick ([dark-factory](dark-factory.md) §Plans; runbook
[dark-factory-arming](../runbooks/dark-factory-arming.md)).

## Where WORK_MANAGEMENT's constraints land

| # | constraint | section |
|---|---|---|
| 1 | the code todo is a `plan_dispatch` root as-is; the gripe mirrors it | §2, §5, §7 |
| 2 | route/risk tags stay out of `child-failed:` / `ask-user:` / `halt:`; a retry drops `child-failed` | §4 |
| 3 | reuse `attempt:N`, `handoff:`, `meta.retries`, the ask at the cap | §4, §5 |
| 4 | main-side and host-side refusals are deferrals, not attempts | §5 |
| 5 | todo → gripe link so the land posts `resolved:` and deploy-verify closes | §1, §5 |
| 6 | feature intake: gripe in, link back, plan tree unchanged | §7 |
| 7 | questions for Reto stay ask-user todos; the gripe shows the blocked state | §5 |
| 8 | explicit executor so dispatch never starts an Opus planner | §3, §9 Q1 |

## 1. The link: `todo → gripe`, `rel='fixes'`

Direction: the code todo is the source, the gripe the target
(`link(kind='todo', id=T, target='gripe:N', rel='fixes')`); the gripe reads
it as `fixed-by`. This is the pair `precis-relations` already documents for
"a workflow job resolves a gripe / todo", the shape `precis-minter-help`
shows for a diagnose todo, and the edge `src/precis/handlers/gripe.py::gripe_close_cascade`
already follows: `_CLOSE_CASCADE_RELS` lists `fixes`, so closing the gripe
posts a close note on the todo today.

The rule table allows it with no change. Constraints live only on the
seeded set `tests/test_relation_constraints.py::SEEDED` (`contradicts`,
`draft-of`, `plan-of`, `dossier-of`, `establishes`, `corroborates`,
`specialises`, `contains`, `has-prerequisite`, `serves`, `selects`,
`rejects`); `fixes` is unconstrained (`test_seeded_values` asserts
`not c["cites"].constrained` for the same reason) and is a registered slug
in `src/precis/store/types.py::Relation`.

Not seeded `functional` (one fixing todo per gripe) on purpose: the
`fix_gripe` lane's job → gripe edge uses the same slug and
`src/precis/workers/dispatch.py::_carry_fixes_link` copies a todo's `fixes`
edge onto every job it mints, so a retry would be refused. One-todo-per-gripe
is enforced at the create door instead (§6 step 2, §7).

The template that mints a gripe-fix plan today links the root
`related-to` the gripe (`src/precis/plans/templates.py::mint_gripe_fix`,
via `_run`'s `relation="related-to"`). Phase 2 changes that one argument to
`fixes`.

## 2. The code root

One **tactical** todo, title `Code work from gripes`, parent td540836 (the
dark-factory root under the platform quest qu540835, `thread:factory`), so
code todos show on `/work` under the factory thread and take the quest's
PRIO flow-down. Created once by Reto or an owner session:

```python
put(kind="todo", text="Code work from gripes", parent_id=540836,
    meta={"tier": "tactical", "llm_tier": None},
    tags=["thread:factory"],
    body="Root of every gripe-driven code todo (docs/backlog/gripe-todo-unify.md). "
         "Not a plan root; each child is its own plan.")
```

Why it stays out of every automatic path:

- Tactical, not strategic: rotation and the 7-day picks count only
  `rotation_root=true` roots (`src/precis/todo/views.py::_active_strategic_ids`),
  so the root is never a rotation unit and its children never feed the
  1/N pick order.
- `llm_tier: None` on the put suppresses the parented default stamp
  (§3); the root is a branch, so `view='doable'` never lists it.
- No `plan:` tag and no gate/loop/generate/auto_close meta, so
  `src/precis/workers/job_types/plan_dispatch.py::_candidate_roots` never
  loads it as a plan. Each code todo under it is its own plan root
  (phase 2) or an inert leaf (phase 1).
- Owner-only tier: a worker source (`PRECIS_SOURCE` `asa-*`) cannot
  create or move it (`precis-todo-tree-help`).

Phase 1 code todos hang directly under this root. From phase 2 a code
todo that fixes a gripe carrying `thread:<slug>` is minted under the root
that carries the same `thread:` tag when one exists (the work view already
hangs gripes there), else under `Code work from gripes`.

## 3. The no-auto-spend guarantee

There is no executor value that means "a human or fleet session does
this": `src/precis/workers/dispatch.py::_claim_and_dispatch` validates
`meta.executor` against the registry and self-halts an unknown one
(`_halt_bad_dispatch`, tag `halt:bad-dispatch`), and `plan_dispatch` never
reads `meta.executor` at all (it picks the executor from the job type's
`compatible_executors`, `_pick_executor`). The guarantee is therefore a
**shape**, written on every code todo and every pass child:

1. `meta={'llm_tier': None}` on the `put`. `src/precis/handlers/todo.py::TodoHandler.put`
   stamps `meta.llm_tier='opus'` on a parented todo only when the caller
   set none of `llm_tier` (the `None` opt-out counts and is stripped from
   the stored meta), `executor`, `schedule`, `auto_check`, `ask`, `op`, and
   the node is not a gate/loop/generator/phase
   (`_PLAN_KINDS_NOT_DISPATCHED`). The ack names the stamp when it happens.
2. Never `meta.executor`, never an `executor:*` tag.
3. Pass children that the factory may run carry `meta.op` (which also
   suppresses the stamp); children a fleet session works carry
   `waiting-for:fleet` (§4).

Proof that dispatch then mints nothing. The dispatch worker's candidate
query `src/precis/workers/dispatch.py::_candidate_parent_ids` requires
`r.meta ? 'executor' OR r.meta ? 'llm_tier' OR` an `executor:*` tag;
`src/precis/todo/guards.py::has_auto_run_signal` states the same rule:
without one of them "the todo is inert — it never spawns a plan_tick
job". The billed path is the branch in `_claim_and_dispatch` that
synthesises `executor = "claude_inproc"`, `job_type = "plan_tick"` from a
bare `llm_tier`; a todo with no `llm_tier` key never reaches it. The same
query also skips any todo carrying an exclusion tag
(`src/precis/store/_todo_sql.py::_DOABLE_EXCLUSION_TAGS`: `halt`,
`halt:`, `waiting-for:`, `ask-user`, `ask-user:`, `child-failed:`) or an
unexpired `claimed-by:` lease (`_doable_exclusion_clause`).

What does spend, and under what brake: in phase 2 the factory tick mints
`run_slice` on `ssh_node` for a doable task node (`_step_dispatch`,
`_resolve_op`, default op `run_slice`). That is the dark-factory lane
Reto armed, under its existing brakes: `PRECIS_FACTORY=off` or
`service_config` prio 0 (`read_only_reason`), lane pacing
(`src/precis/plans/lanes.py::pick_lane`), the retry cap (`meta.retries`,
default 2), and the $25 action cap in the brief footer. No Opus planner
is involved anywhere.

No automatic prod writes: this design adds no write to product data. The
only writes are work bookkeeping on the todo and the gripe (tags,
comments, links), all through the MCP or the tick's existing writers.

## 4. Field mapping and tag vocabulary

| fact | stays on the gripe | moves to the code todo |
|---|---|---|
| intake: title, body, filer, `project:`, `repo:`, `area:` | yes (open filing by any caller) | — |
| dedupe: `meta.dup_parents`, `dup_suggested`, `dup_checked`, `superseded-by` | yes (`gripe_parents` pass, `gripe_triage.parse_verdicts`) | — |
| flood: `flood` tag, `cluster:flood-<filer>` | yes (`gripe_triage._flood_sweep`) | — |
| triage verdict: `fix:`, `cluster:`, `size:`, PRIO, `verdict`, `reason`, `reto:ask` | yes (`gripe_triage._write_verdict`) | — |
| severity | yes (PRIO band) | copied once as the todo `prio` |
| STATUS ladder, `resolved:` close gate, `hold:no-autoclose`, `STATUS:parked` + `meta.until` | yes (`GripeHandler._require_resolved_comment`, `gripe_validate`) | — |
| who holds it | `claimed-by:` mirror only (§5) | `claimed-by:<lease>` (CAS, 4 h) |
| route | — | `meta.task_class` + per-pass `meta.op` |
| risk | — | `risk:*` tags |
| dependencies | comment `parked behind gr<M> (td<U>)` | `blocked-by` links between code todos |
| brief | — | `body=` (`todo_body`, what `run_slice.render_brief` reads) + `brief:` comments for rounds |
| passes | — | child todos (`design`, `build`, `review`, `land`, `verify`) in the plan shape |
| lane attempts: lane, sha, review findings | one mirror comment per transition | `attempt:N` tag, `handoff:` comments, `review: LAND|FIX|REVERT — …` comments, `landed: <sha>` comment, `meta.review` |
| landed sha | `resolved: <sha> (main, release pending)` (land_notify) | `landed: <sha>` + `STATUS:done` (land_notify / `land_slice._record_land`) |
| batch, `meta.fix.{host,session,tree}`, `gripe_brief`/`gripe_handoff`/`gripe_review` chunks | legacy; read-only after phase 1 | replaced by the rows above |

Route vocabulary: `meta.task_class`, one of `design` (frontier model:
Fable), `code` (cheapest lane with fit: Devin, Codex, Kimi, Claude
subscription per the lane table), `review` (another vendor than the
builder, `src/precis/plans/templates.py::REVIEW_TASK_CLASS`), `verify`
(cheapest tier, or a mechanical op when the gripe names a repro), `docs`.
`code`, `review` and `docs` are the lane table's classes today; `design`
and `verify` are added to the Claude-subscription lane with a model pin
(`run_slice.resolve_model`). The orchestrator reads the same field in
phase 1 to pick the vendor it launches.

Risk vocabulary, open tags with one prefix: `risk:spend`, `risk:migration`,
`risk:prod-write`, `risk:public-surface`, `risk:read-only-verb`. `risk:` is
not in `_DOABLE_EXCLUSION_TAGS`, so no risk tag blocks a claim (WM 2). Two
of them change the plan shape instead: `risk:migration` and
`risk:public-surface` insert an `answer` gate (an ask-user `approval`)
between build and land, because migrations and outward-facing actions are
Reto's under `.claude/fleet/authority.md`. The other three change the brief
only: `risk:spend` carries the $25 cap and the paid-fetch rule that
DOIQUEUE (gr551840) missed; `risk:prod-write` says write-path testing is
on the dev DB; `risk:read-only-verb` says the verb must stay read-only and
the test that pins it is part of the slice (the rule MEASFIX gr506762 took
four review rounds to learn).

The fleet lane marker is `waiting-for:fleet`, an existing exclusion-tag
prefix: a node that carries it is skipped by `view='doable'`, by the
dispatch worker and by `plan_dispatch._step_dispatch` (`_excluded`),
while gates, phases and hygiene around it keep running. A retry drops
`child-failed:*` as `plan_dispatch._mint_job` already does (WM 2).

## 5. Lifecycle, end to end

Todo statuses are the todo vocabulary (`open`, `doing`, `blocked`,
`paused`, `done`, `won't-do`); gripe statuses are the ladder. The gripe
never grows a field for any of this: one comment line per transition
(prefix `work:`), and the stage column on `/work` reads the todo.

| event | code todo | gripe |
|---|---|---|
| filed | — | `open` |
| triage: noise / not-a-bug / dup / fixed | none minted | `wontfix` / `superseded-by` / `done` or `auto_verify` (unchanged) |
| triage: bug, `fix:reto` | none until ruled | `triaged` + `reto:ask` (unchanged) |
| triage: bug (phase 1) | none; the orchestrator mints at dispatch (§6) | `triaged` |
| triage: bug (phase 2) | minted `open`, unclaimed, `fixed-by` the gripe, route + risk written | `triaged` + `work: td<T> minted (route <class>, risk …)` |
| triage: `fix:spec` or size L without a spec | minted with a `design` pass first | `needs_spec` until the design pass lands its spec, then `triaged` (existing `scripts/gripe-validate` rule) |
| a pass is claimed (lease, `STATUS:doing`) | `doing`, `claimed-by:lane-<lane>` or `claimed-by:fleet-<tree>` (`run_slice._claim_todo`) | `fixing` (`GripeHandler._claim_status`), lease `claimed-by:plan-td<T>` held while the todo is live (§7 change 3) |
| blocked on another code todo | `blocked-by` link; off `doable` until the blocker closes | `fixing`; `work: parked behind gr<M> (td<U>)` |
| lane attempt red | `open`, `attempt:N`, `handoff:` comment (`run_slice.release_todo`, `land_slice._fail`); the tick retries up to `meta.retries` | `fixing`; `work: attempt N failed on <lane> — <first line of the handoff>` |
| land refused by main or the lander's host (drift guard, disk, compose, gate image, gr552214) | untouched: `land_slice._defer_to_main` parks the job (`main_refusals`, `not_before`, backoff 30 min, cap `MAX_MAIN_REFUSALS`); not an attempt (`_attempts` counts ended jobs only) | untouched |
| retry cap reached | `plan_dispatch._mint_ask`: ask-user todo (`ask-user`, `showstopper`, `plan:<root>`), `blocked-by` from the node; the todo waits | `fixing` + `needs:ruling` (asks-list source) + `work: asks Reto: td<ask>` |
| needs Reto mid-build (`halt:<reason>`, ask-user child with `meta.ask.blocks`) | excluded from dispatch; row on `/asks` | `fixing` + `needs:ruling` + `work: …` |
| ask answered (`answer:` comment, `STATUS:done`, tag removed) | re-armed (fresh retry budget) | `needs:ruling` removed |
| landed on main | `done`, `landed: <sha>` (land_notify from the `(grN, tdT)` subject, else `land_slice._record_land`); the plan's `sha-in-prod` gate gets the sha (`land_slice.pending_gate`) | `auto_verify` (or `user_verify`), `resolved: <sha> (main, release pending)` — written by `scripts/lib/land_notify.py::payloads` |
| partial land (`refs grN` in the subject) | `done` for that slice | `landed (partial): <sha>` comment, status unchanged (`land_notify.partial_gripes`) |
| deployed | gate closes (`src/precis/plans/gates.py::sha_deployed`), the `verify` pass runs, the phase closes | `done` (`gripe_validate` tick); `gripe_close_cascade` posts the close note on the todo |
| never-close gripe (`hold:no-autoclose`) | `done` | stays; `shipped in r<N>` note (`gripe_validate`) |
| gripe closed by a human while the todo is live (dup, not-a-bug) | `gripe_close_cascade` note; the tick or orchestrator closes the todo `won't-do` | `wontfix` with `resolved: wontfix — …` |
| decided not to do | `won't-do` | `wontfix` with `resolved: wontfix — …` (human; the gate stands) |

Reto's park (`STATUS:parked` + `meta.until`) stays a human verb on the
gripe and is never written by the work side; a parked gripe with a live
code todo pauses that todo (`STATUS:paused` on the code todo; pause
propagates to its children).

## 6. Phase 1: the orchestrator mints at dispatch, no triage change

Precondition: the root of §2 exists (one owner call). GRIPE_FIX, when it
would today write a brief on the gripe, does this instead. All calls are
the session MCP; `<ROOT>` is the root's id, `N` the gripe, `T` the new
todo.

1. Read the gripe: `get(kind='gripe', id=N, view='full')`. A gripe at
   `fixing` with a live `fixed-by` todo (the card lists inbound links)
   already has its work item: brief that todo, mint nothing.
2. Mint the code todo, linked, inert, on the fleet lane:

   ```python
   put(kind="todo", parent_id=<ROOT>,
       text="Fix gr<N>: <gripe title, imperative>",
       body="<the brief: slices, files, verify-first, test, rulings, "
            "the standing context rule gr515153>",
       prio=<the gripe's prio>,
       tags=["waiting-for:fleet", "thread:<slug>", "risk:<…>", "attempt:0"],
       meta={"llm_tier": None, "task_class": "code", "retries": 2},
       link="gripe:<N>", rel="fixes")
   ```

   `attempt:0` is optional and only documents the vocabulary; omit it.
   For a two-pass fix (design, then build) mint two children of `T` the
   same way, the build `blocked-by` the design
   (`link(kind='todo', id=<build>, target='todo:<design>', rel='blocked-by')`),
   and give `T` no brief of its own.
3. Dependencies: `link(kind='todo', id=T, target='todo:<U>', rel='blocked-by')`
   for each code todo that must land first, and on the gripe
   `put(kind='gripe', id=N, text='work: parked behind gr<M> (td<U>)')`.
4. Assign the builder. `fleet-wave launch` records who, where and the
   tree on both refs:

   ```python
   tag(kind="todo", id=T, add=["claimed-by:fleet-<tree>"])        # CAS lease, STATUS stays open under waiting-for:fleet
   put(kind="todo", id=T, text="lane: <vendor>:<window>@<host>, tree <tree>")
   tag(kind="gripe", id=N, add=["claimed-by:fleet-<tree>"])       # → STATUS:fixing (GripeHandler._claim_status)
   put(kind="gripe", id=N, text="in progress <tree> — covers all asks; work item td<T>")
   ```

   The builder's brief names the squash subject it must end with:
   `(gr<N>, td<T>)`. The lease is 4 h (`guards.CLAIM_TTL_HOURS`); the
   stuck-watch re-issues the same tags at each poll to refresh it.
5. Handoff: `put(kind='todo', id=T, text='handoff: <text>')`. A red round:
   `tag(kind='todo', id=T, add=['attempt:<n+1>'], remove=['attempt:<n>'])`
   (the shape `run_slice.release_todo` writes) and
   `put(kind='gripe', id=N, text='work: attempt <n+1> failed on <lane> — <first line>')`.
6. Review verdict: `put(kind='todo', id=T, text='review: LAND — <findings>')`
   or `review: FIX — <list>` (the line `plan_dispatch.parse_verdict`
   accepts, so phase 2 reads phase 1 history). A fix round is the next
   `brief:` comment on the todo.
7. Land and close: nothing by hand. The release owner's
   `scripts/fleet-wave land <tree>` runs ship; land-notify moves both
   handles in the subject (todo `done` + `landed:`, gripe `auto_verify` +
   `resolved:`); `scripts/gripe-validate` after the rollout closes the
   gripe. Never a second `resolved:` or a hand `STATUS:done` (gr542762).
8. Questions for Reto: an ask-user todo under `T` with
   `meta.ask.blocks='td<T>'` (`precis-work-help`), plus
   `tag(kind='gripe', id=N, add=['needs:ruling'])` and a `work:` comment.

What the orchestrator stops doing: `edit(kind='gripe', mode='fix',
args={batch, brief, handoff, review, host, session, tree})`. The
`/queue` and `/batch` views go quiet for new work (they read the gripe
chunks); the queue is `get(kind='todo', id=<ROOT>, view='tree')` and
`search(kind='todo', view='attention')`. `edit(mode='fix')` stays
readable for history and is retired in phase 2.

## 7. Phase 2: triage mints; phase 3: features flow the same way

Phase 2 code changes, each one slice:

1. `src/precis/workers/gripe_triage.py::_write_verdict`: when the written
   status is `triaged` (or `needs_spec`) and the verdict is `bug`, mint
   the code todo in the same transaction through
   `src/precis/plans/templates.py::mint_gripe_fix` with the triage node
   skipped (the verdict is the triage) and `fixes` instead of
   `related-to`; skip when the gripe already has an inbound `fixes` link
   from a live todo. The verdict schema gains `route` (one of §4's task
   classes) and `risk` (a list from §4's vocabulary); `parse_verdicts`
   validates both against the closed sets. The todo's `prio` is the
   verdict's PRIO band. A `needs_spec` gripe gets a `design` pass first.
2. `src/precis/workers/job_types/land_slice.py::with_handle`: add every
   gripe id the plan root `fixes` to the squash subject's handle list
   (`(grN, tdT)`), so `scripts/lib/land_notify.py` posts the `resolved:`
   line and `STATUS:auto_verify` on land, and `gripe_validate` closes it
   after the deploy (WM 5). `land_slice` itself still knows only
   `todo_id`.
3. `src/precis/workers/job_types/plan_dispatch.py::_stale_gripe_claims`:
   a `fixing` gripe with a live `fixes`-linked **todo** is not stale (today
   the predicate looks only for a live job under or linked to the gripe),
   so the gripe stays `fixing` between attempts and while an ask is open,
   instead of bouncing to `triaged`. The same hygiene writes the `work:`
   mirror comments on claim, attempt, ask and park.
4. `mint_gripe_fix`: read `risk:*` from the gripe's verdict and insert the
   `answer` gate for `risk:migration` / `risk:public-surface`; carry the
   risk lines into the build brief.
5. `src/precis/plans/lanes.py`: `design` and `verify` task classes with
   their model pins.
6. `src/precis/handlers/gripe.py::gripe_close_cascade` already notes the
   todo; add the reverse: a todo closing `won't-do` while its gripe is
   `fixing` posts `work: td<T> dropped — <why>` and drops the plan lease.

Phase 3, feature intake: today a feature is a root todo under a quest
(td540836 under qu540835) split by a slicer generator
(`src/precis/plans/templates.py::mint_slice_tree`), and nothing files a
gripe. Phase 3 makes the gripe the intake: `feature` joins
`src/precis/workers/gripe_triage.py::_FIX_TYPES`; a `fix:feature` verdict
mints a `slice-tree` plan instead of a `gripe-fix` plan, under the thread
root the gripe's `thread:` tag names (else the §2 root), linked `fixes`
the gripe. Intermediate slices land with `refs gr<N>` in the subject
(partial, no status move); the last one with `(gr<N>)`, so the existing
land-notify and `gripe_validate` path closes the feature gripe after the
deploy. Nothing else about the plan tree changes (WM 6).

## 8. What changes elsewhere

- **`precis-gripe-loop-help`** (product skill, `src/precis/data/skills/`):
  "every state lives on the gripe" becomes "intake, triage and closure
  live on the gripe; work state lives on the `fixed-by` todo". Steps 2 to
  6 point at the todo verbs of §6; step 1 and step 7 stay. The
  `edit(mode='fix')` section moves to a "legacy loop state" appendix.
- **`.claude/skills/gripe-fleet/SKILL.md`**: the loop-step table's
  assign/launch row records on the todo (`claimed-by:fleet-<tree>` plus
  the `lane:` comment) and the gripe lease; the handoff and review rows
  write `handoff:` / `review:` comments on the todo; `fleet-wave launch
  --batch` takes a todo id list (or the root) instead of a gripe batch
  slug; `stuck-watch` refreshes both leases. Brief rule added: the squash
  subject ends `(gr<N>, td<T>)`.
- **Session-start counts**: `scripts/hooks/session-start-memory.sh`
  prints `precis gripes --summary`, which is
  `src/precis/handlers/gripe.py::age_summary` over every non-terminal,
  non-parked gripe (65 open on 2026-11-02, including the six at
  `fixing`). Split the line into `N open · K in work (td) · M older than
  3d · oldest grX`, where "in work" is a live inbound `fixes` link from a
  live todo and the age figures cover only gripes with none. Same helper
  feeds `get(kind='gripe', id='/open')`.
- **Web**: `/gripes/<id>` renders inbound links through
  `src/precis/handlers/_links_render.py::_format_inbound_rel`, which has
  no passive form for `fixes`; add `fixed by`. `/work` gripe rows take
  their lane from the newest job under the gripe
  (`src/precis/todo/work_view.py::_newest_job_lanes`); read it from the
  `fixed-by` todo's newest job instead, and show `td<T>` on the row.
  `/asks` needs nothing: the ask-user todo at the cap and `needs:ruling`
  gripes are already sources.
- **One-off conversion** (Reto runs it, dry run first). Scope on
  2026-11-02: six gripes at `STATUS:fixing` (gr549760, gr550979, gr551098,
  gr551840, gr551238, gr554555), zero in the `/queue` loop, and the
  coordinator's file state under `/tmp/gripe-fix` (a symlink to
  `~/work/gripe-fix-state`, ~1,400 brief and queue files). A CLI verb
  `precis gripe-fix to-todos [--apply]` next to the existing `gripe-fix
  import`: for each `fixing` gripe with no `fixed-by` todo, mint the code
  todo under the §2 root with `body` = the newest `gripe_brief` chunk or
  the matching `/tmp/gripe-fix/briefs/gripes/<id>.md`, copy
  `gripe_handoff` / `gripe_review` chunks as `handoff:` / `review:`
  comments, link `fixes`, and re-issue the gripe's live `claimed-by:` on
  the todo. The dry run prints one line per gripe (id, holder, brief
  source, comment count). Closed and `triaged` gripes are not converted;
  phase 2 triage picks the latter up.

## 9. Open questions for Reto

1. **An explicit executor value, or the inert shape?** WM 8 and the gripe
   ask for `meta.executor` on every code todo. Recommendation: the inert
   shape of §3 (`llm_tier: None`, no executor, `meta.op` on factory
   passes), because an unregistered executor self-halts the todo and
   `plan_dispatch` ignores the field. If a visible marker is wanted
   anyway, the smallest change is a registered `fleet` executor with no
   worker pass plus an exclusion in `_candidate_parent_ids` (two lines in
   `src/precis/workers/dispatch.py`), and `meta.executor='fleet'` on
   fleet-lane nodes.
2. **Where `Code work from gripes` hangs.** Recommendation: a tactical
   child of td540836 (shows under the factory thread, takes the quest's
   PRIO). The alternative is a parentless tactical root, which the work
   view would list as an orphan.
3. **Gripe `fixing` while the todo waits.** Recommendation: a gripe stays
   `fixing` as long as its code todo is live (§7 change 3), so the pick
   list is honest and nobody double-works it; the todo's `work:` comments
   say what it waits on. The alternative keeps today's rule (lease lapse
   returns it to `triaged`), which bounces status at every retry.
4. **Who runs the conversion and when.** Recommendation: Reto, after the
   phase 1 skill change lands, in a quiet window; leases are re-issued
   only for trees still live.

## Explicitly NOT in scope

- A new gripe STATUS value or relation slug; a migration.
- Moving triage, dedupe, flood control or the close gate off the gripe.
- A second scheduler, or any change to how `scripts/ship`, the release
  pass or `authority.md` gate a land.
- Auto-answering asks; reusing harness subscription tokens outside the
  harness (ruled out 2026-10-09).
- Deleting the legacy `edit(kind='gripe', mode='fix')` verbs or the
  `gripe_*` chunks; they stay readable.

## Acceptance criteria

- Phase 1: a gripe dispatched through §6 has one live `fixed-by` todo;
  `search(kind='todo', view='doable')` never lists it; no `plan_tick` or
  `run_slice` job is minted under it (`get(kind='todo', id=T, view='tree')`
  shows no `⚙` row); the land of a subject `(grN, tdT)` closes both
  without a hand write; the gripe is `done` after the release.
- Phase 2: `gripe_triage` on a `bug` verdict mints exactly one code todo
  per gripe, idempotent on re-triage; `land_slice`'s squash subject names
  the gripe; a `fixing` gripe with a live todo survives a lease lapse.
- Phase 3: a `fix:feature` gripe produces a `slice-tree` plan linked
  `fixes`, and closes after its last slice deploys.
- Session-start line shows `K in work`; `/gripes/<id>` shows `fixed by
  td<T>`.

## Target + blast radius

Handlers `todo`, `gripe`; workers `gripe_triage`, `plan_dispatch`,
`run_slice`, `land_slice`; `plans/templates.py`, `plans/lanes.py`;
`scripts/lib/land_notify.py`; `cli/gripe.py`; web `routes/gripes.py`,
`todo/work_view.py`; skills `precis-gripe-loop-help`, `precis-gripe-help`,
`precis-todo-tree-help`, `precis-work-help`; repo skill `gripe-fleet`.

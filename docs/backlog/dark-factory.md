---
status: draft
title: Dark factory — precis runs the work, harness sessions become the exception
pillar: platform
prio: high
---

# Dark factory — precis runs the work

Reto, 2026-10-30 (session WORK_MANAGEMENT): sessions run out of steam and
forget their prompt; the work lives in too many places; builders (Devin,
Codex, Kimi, Claude, castor) are picked by hand; credits expire unused;
the build is cut by hand; the quest surface cannot show science and
platform work side by side. Goal: precis holds the work, dispatches it,
picks the builder, keeps the build cadence, and shows one hierarchy. The
harness (Claude Code, Codex, Devin CLI) stays an engine we start with
`-p`/`exec`, not the place state lives. Not a systematic plan: the next
steps, ranked by how many later steps each one unlocks.

## What already exists (do not rebuild)

- **Loop state on the item.** The gripe-fix loop keeps brief, handoff,
  review and landed sha as chunks on the gripe; the queue is derived
  (`precis-gripe-loop-help`, `scripts/fleet-wave`, `precis gripe-fix`).
  This is the "next prompt from spec + last landing" mechanism, for
  gripes only.
- **Agent registry.** `kind='fleet'`: one row per vendor×host×tree,
  state, ctx %, purpose, quota per vendor; reporter per host
  (`fleet-coordination-via-precis.md`). Empty today: the launchd reporter
  is not installed on this Mac.
- **Hierarchy with leases.** The todo tree: strategic roots, `doable`
  leaves, `claimed-by:` leases, `part-of` (`precis-todo-tree-help`).
  Quests: PRIO flow-down over `serves`, WORM logbook, ticks
  (`precis-quest-help`). Asks: `ask-user` todos with `meta.ask`
  (td541979; no review-queue files), web `/asks` and the Needs-you
  landing (`routes/asks.py`).
- **Credits.** `budget.quota` (Claude windows via the `claude -p` probe),
  the dollar breaker, the work-class taper (`budget-guardrails.md`,
  design-of-record: guide rails + one breaker, no per-task accounting);
  Codex `rate_limits` and Kimi request counts in the fleet reporter.
  Rulings: smallest model first (me538286), spend the whole weekly quota
  before reset (me491356).
- **Routing.** `utils/llm/requirement.py::choose_model`,
  `policy.py::select_offering` for LLM calls; nothing routes a *builder
  session* to a vendor — that is the GRIPE_FIX coordinator's judgment
  plus the lane memory (me538174).
- **Build cadence.** `scripts/round`, `scripts/release`, `scripts/deploy`;
  the release owner runs them by hand; one release at a time; the deploy
  lock; `scripts/board` for status lines.
- **Authority.** `.claude/fleet/authority.md` v2: tiers R/W generated
  into settings, Codex flags and Devin config; Reto-only list; drift
  test. Harness diffs (`HARNESS_DIFF` in `scripts/ship`) land only under
  the full gate.
- **Self-fix ladder.** `diagnose_scan` → fixer, arming rungs, kill switch
  by `service_config` prio 0 (lane OFF since 2026-10-02;
  `dark-features-activation.md`, me474348). Halt classes and the outage
  breaker are specced (`halt-classes-and-outage-breaker.md`).
- **Repo prose indexed read-only** as the `md` kind (`main/docs/...`,
  frontmatter included).
- **Context.** Session MCP-TOKEN-FIX's rulings (2026-10-30): the twin
  `context_items` keyed on `_ctx`, handle sets, row grammar, Mode B
  managed context with plan_tick first. Mode B renders a runner's prompt
  from the store each cycle, so factory runners never compact and never
  forget; harness compaction only matters for Reto's interactive desks.

## Decisions (Reto 2026-10-30)

- **One work tree, quests included.** Pillars become quests (B work is
  self-improvement of precis); thread ordering becomes PRIO on the slice
  todos under the quest; the thread file's Resume block becomes the
  slice's handoff chunk. Backlog spec files stay in the repo (prose; git
  is their history) and are read through the md kind. `docs/roadmap.md`
  and `docs/backlog/threads/*` retire per thread as their rows move,
  under the existing retirement rule. DRY: a fact lives in the quest or
  the spec file, never both. Order: mint the pillar quests and `serves`
  links first (data, reversible), the `precis-work-help` skill in the
  same release, then move thread state incrementally as slices land. No
  big-bang migration.
- **Judgment roles get the strong model, builders the cheapest.** The
  slicer (spec → slices, briefs) and the reviewer run on a frontier
  model; the builder is the cheapest lane with fit. Reviews cross
  vendors: the reviewer never comes from the builder's lane.
- **Release on event, not timer.** When the previous release is done
  and new shas are on main, cut the next one. One open release at a
  time; latecomers ride the next.
- **Ceilings, not budgets.** No per-task token budget. A slice has a
  context ceiling (handoff at 70 %), an action spend cap ($25), and the
  lane has its pacing target. The global breaker stays the only stop.
- **Showstoppers, decisions, discussions are one list** with three
  classes, grouped by quest/pillar, visible to every user; multi-user
  argument is comments on the row, later. Gripes flow into it: a gripe
  that needs a ruling is a decision row, one that blocks a lane is a
  showstopper, one that argues a shape is a discussion. Nothing else is
  asked of Reto.
- **Papers and findings run the same loop.** A paper is a quest; its
  slices (citation pass, export, sign) are todos with loop chunks; the
  lane may differ (a writing slice goes to a frontier model). The
  science ticks already run this way; this adds the human-shaped slices.
- **Hard rules stay code.** Gate, ship, release, deploy, authority,
  qgo-guard, migration rules: scripts and tests, never prompts. The
  factory chooses who builds and what the brief says; it never softens
  how a change ships.

### Round 2 (Reto 2026-10-30, later the same day)

- **A paper is not a quest.** Quests never finish; a draft gets posted.
  The shape in prod stands: qu459585 (the cadence quest) → one month
  todo per paper (td459586 October) → the draft `draft-of` it; the draft
  `serves` every science quest it draws on (many parents, which `part-of`
  cannot give). The paper's slices (citation pass, export, sign) are
  children of the month todo. The quest feature Reto misses on a draft
  is the mindmeld turn (Round 3), which is a turn, not a kind.
- **Pillar quests tick, as build ticks.** A pillar quest is `active` and
  its tick does not research: it reads the specs and gripes that serve
  it, breaks them into slices, mints and dispatches them (a short
  research step is allowed first). Same quest-tick loop, a second role
  set: slice / build / review instead of demand / supply / bridge. So
  the quest kind needs a tick type, not a dormant exception.
- **Use-up wins over balance.** Every windowed lane is paced to reach
  its reset or deadline fully spent. There is always work; an idle lane
  with credits falls back to memory-graph improvement (the gardener's
  idle-filler role, generalised from local compute to agent lanes).
  Reto, Round 3: this is the valve for credits that must go in a hurry;
  the expected state is work outstripping tokens, so the filler rarely
  runs.
- **Model per task, not per quest.** A table from task class to tier:
  trivial → any free lane; difficult code → Opus; complex planning and
  slicing → Fable; review → a frontier model from another lane. The fit
  score refines within a class; it never promotes a trivial task to a
  paid tier.
- **No objective function.** "Self-improving" means the loop fixes
  gripes, ships features and does research with less human steering;
  more capability is the direction. The fit rollup is the only learned
  number; nothing optimises a global score.
- **The coordinator is short-lived.** It boots fresh every hour or
  five, reads the work tree, asks and board, acts, exits. Fresh boot
  from state replaces compaction; Mode B is not needed for it. Long
  compacted coordinator sessions end.
- **Groups follow the quest tree.** Showstoppers, decisions and
  discussions group by the quest (science or pillar) and, under it, the
  thread or feature that is broken.
- **Stuck gripes are argued in chat.** A gripe blocked on a human
  decision is a decision row; the argument happens in the chat turn on
  that row, with Reto; the outcome is a ruling comment on the gripe and
  the row closes.

### Engine: `claude -p` or direct access?

Three doors, by what they buy:

| door | credits | control over the prompt | attribution |
|---|---|---|---|
| `claude -p` / Agent SDK (the harness headless, setup-token) | the plan: the only door to subscription credits | system prompt, tools, MCP config, `--max-turns 1`, JSON output | PreToolUse hook sets `_ctx` |
| Messages API with an API key | metered dollars at list price | total: Mode B owns every byte | the router sets `_ctx` |
| local / OpenRouter through the router | slots / dollars | total | the router |

The plan's credits are reachable only through the harness; the
2026-10-09 ruling against reusing plan tokens in a home-built loop
stands. So the dark lane's Claude slices run `claude -p` (already the
router's OAuth transport and the `sandbox_run` path), with
`--max-turns 1` when Mode B owns the prompt, as MCP-TOKEN-FIX's B3
already names. Direct API is for metered and local lanes, where it is
what the router does today. Codex, Kimi and Devin: their CLIs, same
reasoning. Verify before building: that the setup-token headless path
is within each plan's terms at the volume the lane will run, and
whether the Agent SDK (same binary, in-process control) may run on a
subscription at all; its docs name an API key.

**Not reverse-engineering the harness** (Reto asked, 2026-10-30; ruled
no, same as 2026-10-09). Taking the setup-token and calling the Messages
API with the harness's headers would buy only the harness prefix (its
system prompt and tool block, a few thousand tokens that prompt caching
already makes cheap after the first call of a window). It costs: the
subscription token is licensed for the harness only, Anthropic has
blocked third-party clients on that ground, and a ban lands on the one
account that funds every Claude lane and Reto's interactive desks; the
header and client checks change without notice, so the lane breaks on
their schedule. The measurement that closes the question instead: run
one trivial slice through `claude -p --max-turns 1 --output-format json`
with `--system-prompt`, `--tools`/`--strict-mcp-config` and
`--setting-sources` stripped to the brief, and record the per-turn
overhead in tokens and seconds against a bare Messages call. Under a
tenth of a slice's spend, closed; above it, the saving goes into
shorter briefs, not into a home-built client.

### Round 3 (Reto 2026-10-30, evening)

- **The mindmeld turn** (answers Q1). What Reto misses on a draft is one
  activity: "quiz me and make sure we are on the same page". It is a
  turn kind, not a quest feature, and it hangs on any work node: a
  draft, a slice, a spec, a quest. The turn reads the node's spec
  section, the previous handoffs and the landed diff, and asks the
  questions where its model of the work could diverge from Reto's,
  numbered, each answerable by yes, no or a correction. The questions
  are discussion rows in the asks list; the answers land as a `rulings`
  chunk on the node, and the next brief includes it. Who asks for one:
  Reto, from the row or the chat; the slicer, when a spec section
  leaves a brief underdetermined (the turn is then the slicer's exit,
  the slice waits as a decision row). A mindmeld never blocks a slice
  unless the asker marks a question as a showstopper. Drafts get their
  logbook from revisions and their PRIO from `serves`; with the mindmeld
  turn there is nothing left that would make a draft a quest.
- **One priority** (answers Q2). Quest PRIO is the only priority; the
  flow-down writes it into the todo `prio` column, the 1/N root
  rotation goes, the dispatcher reads one number. Design work before
  the build: how a slice serving two quests resolves (proposed: the
  hotter), what the rotation's fairness guarantee becomes (proposed:
  nothing; use-up pacing plus the showstopper list replace it), what a
  todo root without a quest ancestor gets (proposed: the coldest band
  and a smell line on `/work`), and whether the flow-down runs in the
  quest tick or as a trigger on PRIO change.
- **Launch from the page, as a job** (answers Q11). The chat turn on
  `/work` may launch a slice. State management is split and already
  exists in shape: the todo holds the *work* state (what, prio, lease,
  loop chunks); a `job` holds the *run* state, with the status
  vocabulary queued / running / succeeded / failed / cancelled, retry,
  idempotent submit, node pinning, `waiting_ask_user`, and the `link` +
  `rel` tie to its parent (`precis-job-help`); the fleet row and the
  agentlog hold who and where. New: one job type, `run_slice`, tied
  `link='todo:<id>', rel='builds'`; its runner is the dispatcher of
  step 6 on a host that has the lane's credentials. So the web process
  never spawns anything: the turn enqueues the job, the dispatcher
  launches, and launch authority stays where the credentials are.
  Cancel from the page is the job's existing cancel.

## Lanes: capacity shapes

| lane | capacity shape | pacing rule |
|---|---|---|
| Claude subscription | 5 h + weekly windows, `used_percentage` | use it all by reset; the taper already stops paid tiers near the ceiling |
| Codex | weekly `primary` window | same |
| Kimi | monthly pool, deadline, heavily used | target rate = remaining ÷ time to deadline; prefer Kimi while it is behind target, stop at the pool |
| Devin | no pool; a concurrency cap and observed rate limits | fill to the cap; back off on throttle; treat as free |
| metered (OpenRouter, API keys) | dollars | only when every free/subscription lane with fit is full; dollar breaker |
| castor local | slots | docs/typo only until a fit record exists |

Pick: fit for the task class first; among fit lanes the one most behind
its pacing target; metered last. Fit learns from the loop (first-round
`LAND` raises, `FIX` or a revert lowers), rolled up nightly.

## Branches: who merges what, how

Reto, 2026-10-30 (Round 3): squash merges, reviews, deploys, automatic,
and no detritus branches. **Ruling, same evening: land the moment a tree
is feature-complete and reasonable; all checking happens on the batch at
the release.** Per-tree gating serialises N full runs on one Docker VM;
a batch costs one run plus two or three fix rounds whatever N is;
nobody deploys `main`, prod comes from `release/r<N>`, so a red `main`
hurts no one, while an unmerged finished tree drifts and is forgotten.
What exists: `scripts/ship` already squashes
(`commit-tree` + CAS push, one message), merges `origin/main` forward
first, then resets the branch to the shipped `main`; worktrees that are
merged, clean and sessionless auto-reap; `*-pin` branches with no
worktree are left alone ("stranded", hundreds today); no pull requests
except Devin's; review is Reto's queue plus the reviewer agent. The
factory keeps the machinery and adds an owner to every branch.

- **A branch is a slice.** The dispatcher creates `slice/<todo id>` from
  `origin/main` in a fresh worktree or `sandbox_run` container when it
  launches the slice; the branch name is the slice id, the slice's
  `handoff` chunk records the tip sha. No branch exists without a slice,
  and a slice has at most one branch. Reto's desks keep their named
  trees; everything below applies to factory branches only.
- **The builder pushes to its branch, nothing else.** Enforced by the
  credential, not the prompt: the lane's token or deploy key may push
  `slice/*` refs only; `main`, `release/*`, `gated` and `prod` take the
  lander's credential. Devin's pull request is just its branch; the
  lander lands the branch and closes the PR, nobody presses the GitHub
  merge button.
- **Landing is immediate: builder done → `scripts/ship`.** The lander
  (the release-owner daemon of step 4, a W-tier runner until it exists)
  lands the branch as soon as the handoff says done, with no verdict to
  wait for. Checks at land are the ones that cost seconds and run
  locally: the subject check and ruff format. mypy, import contracts,
  the hygiene tests and pytest all move to the batch; that needs a ship
  mode rawer than today's `--quick`, whose blocking lint took over ten
  minutes on a prose-only diff on 2026-10-30 (Tailwind rebuild
  included). The squash message is the slice title and brief. A merge
  conflict on the forward merge is a fix round for the builder lane, not
  a human. After the land the lander writes the `landed` chunk and
  **deletes the branch and its worktree or container**, instead of
  ship's reset to `main`: the slice is done, so the branch has no owner
  left. Human trees keep the reset.
- **Review after land, non-blocking, from another lane.** The reviewer
  (lane table `exclude=` the builder's vendor) reads the slice's squash
  diff on `main` against the brief and writes a `review` chunk: `LAND`
  (clean), `FIX` (with the list) or `REVERT` (with why). `FIX` mints a
  fix slice in the same batch; `REVERT` mints a revert slice, the
  original slice requeues with the verdict in its handoff. Landing never
  waits on the review; attribution per slice stays. Reto reviews only
  what authority marks Reto-only (harness diffs, migrations,
  `safe_fetch.py`, authority, loosened tests); those are the one class
  that still waits before land, as decision rows in the asks list with
  the diff handle.
- **The batch gate is the release pass, and it loops** (step 4):
  previous release done, new shas on `main` → cut `release/r<N>` at the
  head, run the full gate once for the whole batch. Red → the fixer
  slice gets the failing test list and the commit range (ten squashes
  bisect in minutes, each names its slice), its fix lands on `main`,
  the pass re-cuts r<N> at the new head and gates again. Two or three
  loops is the expected cost; three red loops in a row park the release
  as a showstopper. No long-lived release branch, no cherry-picks.
  Green → deploy pinned, close `awaiting-deploy` rows. Migrations and
  `safe_fetch.py` stay on the human `/go`.
- **The drift guard must follow the cadence.** `scripts/ship` refuses
  qlands once `main`'s last green matrix is 48 h old. Under batch landing
  that fires whenever two days pass without a release, so either the
  release pass runs often enough that it never trips (the intent), or
  the threshold is expressed in builds, not hours, like the review
  protocol's distances. Code change in `scripts/ship` (harness diff,
  Reto's lane), not a prompt.
- **Detritus has one rule: no owner, no branch.** The reaper runs with
  the release pass: a `slice/*` branch whose slice is `landed`,
  `abandoned` or missing is deleted (local and remote); a stranded
  `*-pin` branch whose tip is contained in `main` is deleted; one not
  contained is listed as a discussion row once, deleted after a grace
  of three builds unless a slice claims it. A crashed slice leaves no
  orphan: the ctx-guard or the runner writes the handoff from the branch
  state, and the branch stays owned until the slice is requeued or
  abandoned.
- **Tags and receipts are the record, not branches.** `release/r<N>`
  is kept (receipts in `scripts/round`); `main`'s squash commit names
  the slice id and the review verdict in its trailer, so the branch can
  go.
- **Status follows the land (gr541800, shipped).** Gripe ladder: triaged
  → fixing (a `claimed-by:` lease writes it) → verify → done. After the
  CAS push `scripts/ship` moves every `(grN, tdM)` handle ending the
  squash subject: gripes to verify with `resolved: <sha> (main, release
  pending)`, todos to done with `landed: <sha>`; after the rollout
  `scripts/release` runs `scripts/gripe-validate`, which closes the
  verify gripes whose sha is on `origin/prod`. Still open: the /gripes
  views and the work-view stage cell reading STATUS (td541982).

**How it breaks, and the guard:** a builder with the lander's
credential → separate tokens, drift-tested like authority; a red batch
nobody can attribute → every squash names its slice, the fixer gets the
range and the failing tests, bisect is cheap; reviewer and builder from
one vendor → `exclude=` is mandatory for the review role, a missing lane
table entry refuses the launch; the reaper deleting a human's branch →
it touches `slice/*` and stranded `*-pin` only, never a branch with a
live worktree or session; a `REVERT` loop → the fingerprint breaker of
step 7 (three reverts park the slice as a showstopper); a batch that
never goes green → three red loops park the release. **Open:** whether
to keep Devin on pull requests at all or give it a branch credential
like the others; the grace period for stranded branches (three builds
proposed). Answered: the lander lands everything the moment it is done,
with the seconds-cheap checks only.

**Flow review, Reto 2026-10-31 ("make it work and we dogfood it").**
Four rulings, built as wave 5: (1) the builder never ships — it commits
on `slice/<id>` and ends with the handoff; the `land_slice` job is the
lander, so the deny-path check runs before the land, not after
(td543731); (2) review is a node in every template that lands code, a
`reviewer` generator blocked by the build and blocking nothing — the
gate and validate follow the land, the verdict mints FIX and REVERT
tasks (td543732); (3) the release cut returns the moment the push is
done and a verdict poll on no lane records the GitHub result, so the
`tests-green` gate does the waiting; cut and deploy are mechanical ops,
only the fixer is a model slice, and the deploy sits behind an `answer`
gate because authority.md keeps deploys Reto's (td543733); (4) the tick
dispatches by `(prio, id)` across all roots — a prio-1 release fixer
beats a prio-5 gripe build (td543732). Priority between gripes and
ships is therefore the todo's `prio`, not a lane. The builder credential
(push `slice/*` only) is a GitHub ruleset plus a second account or
deploy key, Reto's (ask td543734).

## Plans: the job lifecycle and the plan graph (step 3 design, td541983)

Reto 2026-10-30: every gripe goes triage → fix → validate, each action
an LLM call (triage picks the model and may ask; fix may be a frontier
model writing the brief for a cheaper builder; validate waits for the
sha to reach prod, checked on a schedule). The same list type must
express "five in parallel, then 6 and 7 in sequence, then three in
parallel, then dogfood and cycle a few times", a coordinator sets it up,
spec writers are generated on demand, and the PCB generator runs on the
same machinery. The design adds no kind: a plan is a todo subtree, and
the dispatcher is one schedule tick over it.

### Node kinds, on existing fields

| node | how it is written | what completes it |
|---|---|---|
| task | a leaf todo with a brief (step 2 chunks); `meta.op` = job_type + params, default `run_slice` for LLM work | its job ends: a land writes STATUS:done (ship, td541978); a mechanical job writes it on success |
| phase | a todo with children and `meta.auto_close` | every child done or won't-do; the tick closes it |
| gate | `meta.gate = {kind, args}`; kinds `sha-in-prod {sha}`, `tests-green {range}`, `answer` (an ask-user todo is a gate), `not-before {ts}`, `drc-clean {board}` | the tick evaluates the predicate; nothing is dispatched for a gate |
| loop | `meta.loop = {max, until: <gate spec>, body: <template>}` | the tick re-mints the body when every current child is done, `until` is false and count < max; exit closes it |
| generator | `meta.generate = {role: slicer \| spec-writer \| triage, from: <md section \| gripe \| board>}` | its job is an LLM turn whose output is child todos; afterwards it behaves as a phase |

Edges: containment is `parent_id`, sequence is a `blocked-by` link
(`view='doable'` already honours it), service is `serves`. The example
reads: phase A holds five tasks with no links among them; phase B is
blocked-by A and holds 6 and 7 with 7 blocked-by 6; phase C is blocked-by
B and holds three tasks; loop L is blocked-by C, its body is [a dogfood
task, a fixer generator over gripes tagged `plan:<L>`] and its `until`
is "no open gripe carries that tag", max 3. The `plan:<id>` tag is how a
cycle keeps its own findings together.

### Job lifecycle per task

The job substrate stands as it is: queued → running → succeeded |
failed | cancelled, with `waiting_*` for parks (coordinator `Yield`
checkpoints, `defer_on_pause`). The todo mirrors it: `doing` while a
live job or lease exists; on failure the runner posts the handoff chunk
and the todo returns to `open` with `attempt:<n>`; past `meta.retries`
(default 2) the tick mints an ask-user todo that blocks it — a
showstopper row — and stops. A lease older than 4 h with no live job is
released. Lane, model and effort are fields on the job.

### Templates

A template is a function in `src/precis/plans/templates.py` that mints
a subtree; `put(kind='todo', mode='plan', args={'template': …})` or the
coordinator turn calls it.

- **gripe-fix(gripe):** triage (generator, cheapest model: task class,
  model, and an ask gate when the report is unusable) → brief (task,
  frontier model writes the builder's brief chunk) → build (task,
  `run_slice` on the picked lane; the land writes `resolved: <sha>` +
  verify on the gripe) → gate sha-in-prod → validate (task: re-run the
  named repro or test when the gripe names one, an LLM read otherwise)
  → done on the gripe. The gripe keeps the ticket state (gr541800
  ladder); the subtree does the work and is `related-to` the gripe.
- **slice-tree(spec section):** spec-writer generator when the section
  is thin → slicer generator → N tasks each with a declared file set,
  overlapping sets sequenced by blocked-by at mint (gr541684) → review
  task on another lane → phase auto-close.
- **release-pass:** cut (`release_cut`) → CI verdict poll
  (`ci_verdict_poll`) → gate tests-green → approval ask (`answer` gate,
  `deploy r<N>` | `hold`) → deploy (`release_deploy`); fixer generator on
  red → re-cut, the loop from "Branches". Open: the loop's re-cut is still
  an LLM task that does not re-stamp `meta.release` or re-arm the poll.
- **pcb-pipeline(board):** place+route (`pcb_place`, placer topo, route
  on) → drc (`pcb_review`'s check leg) → review generator over the DRC
  report minting fix tasks → loop until drc-clean, max N. The
  workbench ops that are not job_types yet become ones; nothing else
  changes for the PCB code.

### The dispatcher tick

One schedule pass (a `plan_tick`-shaped job, `dedupe_pending`, the
step 7 kill switch): (1) evaluate gates; (2) advance loops; (3)
auto-close phases; (4) for each doable task with no live job, pick the
lane (fit, then pacing), mint the job with the rendered brief, launch
through the lane's adapter (step 6); (5) hygiene: expired leases, retry
caps, stuck jobs → asks. Concurrency is the lane cap. The work view
shows gates and loops as rows with their predicate and count.

### What exists, what is missing

Exists: the todo tree with `doable` and leases; blocked-by; `plan_tick`
(a parented put already synthesizes a planner tick from
`meta.llm_tier`); the coordinator executor's `Yield` with checkpoint
and wake condition; `quest_tick` as a perpetual loop; `good_search` as
a fan-out with triage children; `pcb_place`, `pcb_review`, `pcb_argue`;
`deferred_llm_call`.

Landed 2026-10-30 (wave 3, all `--quick`, untested until the next
`/go`): `precis.plans.nodes` — the five shapes validated on todo put
and edit (d81a33bb7); `precis.plans.gates` — the four evaluators, with
`scripts/release` recording `{head, deployed_at, landed}` into
`app_settings` key `release.prod_head` after a deploy (9a68887b0);
`precis.plans.lanes` + the `run_slice` job type — static lane table,
detached submit/poll on `ssh_node`, `claude -p` in a `slice-td<id>`
worktree, handoff from the last message, `attempt:<n>` on failure
(db4ff2754); `precis.plans.templates` — the four templates behind
`put(kind='todo', mode='plan')`, no `llm_tier` default on gate / loop /
generator / phase nodes (02fb10926); the `plan_dispatch` tick — gates →
loops (`BODIES` registry) → auto-close → dispatch → hygiene, kill switch
`PRECIS_FACTORY=off` or `service_config` prio 0, **not armed in prod**
(d36ca88f1; the arming `put` is in `precis-job-help`).

Landed 2026-10-31: the permission ruling (td542236: `bypassPermissions`
plus per-lane `deny_write_paths`, enforced by `--settings` deny rules in
the session and a poll-time diff check on the server) and the `run_slice`
hardening — branch `slice/<id>`, env allowlist with no DSN, boot_id in
the handle (a97d9154c). **Armed in prod the same day:** the
`plan_dispatch` tick dry-ran as recurring td543630 (nine passes, each
naming the triage node of gripe-fix td543631 on gr524267 as the slice it
would dispatch), then went live as td543894 once r161 carried a97d9154c;
`gripe_validate` as td543715. **The first live dispatch (jo543902,
13:05Z) failed at launch:** an arbitrary cluster worker claimed the
`run_slice` row and had no git checkout around its installed package.
The live tick is paused until the capability slice below is deployed and
a host advertises the slice capabilities (ask td543961: `PRECIS_SLICE_ROOT`
and `PRECIS_CLAUDE_BIN` in the slice host's worker environment).

Wave 5 (Reto 2026-10-31, "make it work and we dogfood it"), the four
rulings from the flow review plus two more, all landed the same day:
the **lander** — the builder commits on `slice/<id>` and stops; the
poll's deny check passes and a mechanical `land_slice` job runs
`scripts/ship`, writes the sha into the gate blocked-by the landed todo
(and only that one) and deletes branch + worktree (87f2a8dbf,
c215f56fd); **review after land** as a `reviewer` generator node in
gripe-fix and slice-tree, verdict `review: LAND|FIX|REVERT` handled by
the tick, FIX mints a fix task, REVERT a revert task and resets the
sha-in-prod gate, dispatch ordered by `(prio, id)` across all roots
(152d3a055, a77bec3f7); the **dark release pass** — `scripts/release
--cut-only` returns after the push, `ci_verdict_poll` parks itself while
CI runs, an `answer` gate keeps the deploy Reto's, `release_deploy`
refuses unless the answer names its release, a red verdict re-cuts on
the first cut's host and restarts the poll (8e689d950, d18e809c4,
fb5b52048, b6eaee5bf); the release modules in `deny_write_paths` and the
cut/deploy host pin (d004db304); **capability placement** (Reto: "the
host that can do it can take it, and alert if none can") — the factory
job types declare `ServiceSpec.placement` tokens (`run_slice`:
repo_checkout + claude_bin + git; lander, cut, deploy: repo_checkout +
git), the heartbeat probes them into `resource_slots`, the claim gate
places the row, the sweeper's `unschedulable` alert fires when no single
live host has them all, the tick notes a job queued over ten minutes
(5b1ab776b); **PR intake** per step 9 — `pr_poll`, the class rules in
`precis.plans.pr_class`, `dep_check` (relock + resolve under 3.12 and
3.13, never building), `pr_action` behind an answered ask, `land_slice`
closing the PR (70637167c). Four of the ten open Dependabot PRs are
majors (trafilatura too), so four approval asks will appear when
`pr_poll` is armed. Open: the builder credential that may push `slice/*`
only (ask td543734); `scripts/ship` refusing a slice diff on deny paths
pre-land (td543697, harness); land-notify losing the ship's note under
load (gr543833, twice on 2026-10-31); analyst brief close and PR head
pin / re-intake (td543919, td543920, building).

## Next steps, by unlock value

Each step is its own `###` heading so a dispatch brief can address it
through the md kind (`…~1b-the-asks-list`, or the step title) — the old
numbered list had no `~` selector (gr541666).

### 1. One work tree, read-only first

(one build). `get(kind='todo', view='work')` + web `/work`: quest
(pillar or science) → thread/spec (md frontmatter: `status`,
`claimed-by`) → slice/gripe (loop queue) → builders (fleet rows per
tree, count and vendor) → landed/deployed (round refs). Gripes hang
where their `thread:` tag puts them, untagged ones in one bucket.
Columns: status, agents on it, age in builds, the open release and its
gate. Unlocks 2–8. Same build: install the fleet reporter so the tree
has agents in it.

### 1b. The asks list

(small, reads 1). One view and page with three classes — showstopper
(a lane is stopped on it), decision (a next slice waits on it),
discussion (a shape question) — grouped by quest, fed by `ask-user:`
todos, `waiting-for:reto`, review-queue files, gripes tagged
`needs:ruling`, red releases and tripped breakers. Replaces the review
session's gather step. Argument = comments on the row; `all users see
all` for now. **Built (td540847):** `view='asks'`, `/asks`, Needs-you;
ask-user, `waiting-for:reto`, `needs:ruling`, `discussion` and
review-queue files are sources. **Open:** red releases and tripped
breakers (no source until step 4), mindmeld questions (Round 3's turn),
live tmux dialogs.

**Folded gripe gr541665 — review-queue `open/` was invisible to the
server.** td540847's asks view read `PRECIS_REVIEW_QUEUE_DIR` or a
checkout's `.claude/fleet/open`; neither exists in the deployed image
or a worktree (`open/` is not in git), so prod saw zero review rows.
Decided here: review items persist in prod as ask-user todos with
`meta.ask` (td541979) and the directory retires.

### 2. Loop chunks on slices, not only gripes

(one to two builds). `edit(kind='todo', mode='fix',
args={brief|handoff|review|landed})` with the same derived queue; a
`brief` render = spec section (md kind, `~heading`) + the previous
handoff + the landed diff's summary + the authority footer. A slice is
one session: brief → build → handoff → exit; the next brief is
generated, never carried in a session. The fix for "forgets its
original prompt". The slicer role (frontier model) writes slices from a
spec; the reviewer role writes verdicts; the mindmeld turn (Round 3)
writes the `rulings` chunk, from Reto's numbered answers.

### 3. Lane registry + pick

(one build, then learning). Table `lanes` per the capacity table above,
seeded by hand from me538174; the nightly fit rollup; `exclude=` for
cross-vendor review. Unlocks the agent dashboard and the
self-improvement loop.

### 4. Release pass, event-driven

(one build + one ruling). A worker pass: previous release done, new
shas on main, no release open → cut `release/r<N>`, gate, deploy
pinned, close `awaiting-deploy` rows. Judgment steps (verify the live
product, `--confirm-runtime`) stay with a session until the dogfood
verdict is mechanical. **The ruling it needs:** authority.md makes
release and deploy Reto-only except the release owner; a daemon as
release owner is a change to that file (Reto's commit). **How a
home-built release loop breaks, and the guard for each:**

- a red gate nobody reads → the pass mints the fixer slice with the
  failing tests and the commit range, re-cuts when the fix lands (the
  loop in "Branches" above); three red loops in a row make the release
  a showstopper row (1b) and the pass stops cutting;
- CI-green, runtime-broken → post-deploy probes stay mechanical
  (health checks, the alert board) and a failed probe redeploys the
  previous `origin/prod` sha; two rollbacks in a row trip the breaker
  and the pass parks itself;
- migrations and `safe_fetch.py` → stay on the human `/go` lane
  (`scripts/qgo-guard` already refuses them);
- two cutters racing → the existing deploy lock and one-open-release
  rule; the pass holds the same lock the session does;
- the pass deploying the code that runs the pass → the pass runs from
  `origin/prod`, not from the sha it deploys; harness diffs
  (`HARNESS_DIFF`, extended to the factory's own modules) never ride
  the dark lane;
- a landing cadence so fast releases never settle → bounded by gate
  time; if that proves wrong, a minimum spacing, not a timer.

**Folded gripe gr541800 — the gripe ticket ladder.** Reto, via the
/gripes web form: work from the `triaged` list without watching the
big one; wanted a `fixing` state (claimed, in flight) and a
wait-for-validation state carrying the landed sha. `verify` exists
since migration 0247, but with pipeline-v2 semantics — fix deployed,
awaiting the probe verdict — which covers "on prod" rather than "on
main awaiting release". The WORK_MANAGEMENT proposal maps the rest
onto this spec: `fixing` = the CAS claim lease written at claim;
`verify` = on main awaiting the release pass, sha in the `resolved:`
comment; `done` = flipped by this pass; `ready_for_fix` and
`in_review` collapse under land-then-review. **Open (Reto):** whether
`fixing` is a new STATUS value (vocab migration + 0176 trigger +
every enumeration gripe-pipeline-v2 slice 1 lists) or stays the
derived `in-build` queue — `gripe-fix-loop-on-the-gripe.md` ruled the
derived queue suffices for the loop, so the question is what the
ticket axis itself must show (`/gripes` default filter, a verify
list, the td541982 stage cell). Either way `verify` needs one
ruling: on-main-awaiting-release vs deployed-awaiting-probe.

### 5. Pillars as quests

(half a build of data, one build for the tick type). Mint one quest per
pillar, link each thread root `serves` it; the quest board shows
science quests and pillar quests in one ranking with the same PRIO and
logbook. The pillar's "where it stands" line becomes a logbook entry;
`docs/roadmap.md` shrinks to a pointer and retires. The build tick
(role set slice / build / review) is the quest-loop change; until it
lands the pillar quests stay `dormant` and the board marks them as
such, not as stalled. `web-quest-editor.md` and
`quest-loop-cadence-strip.md` are the surface items this feeds.

### 5b. List, click, chat

(one build, after 1). The design-workbench pattern on `/work`: rows are
handles; a click selects, the chat form posts message plus handles; the
turn runs with the fisheye of the selection plus the asks and lanes
touching it; its ops are the loop verbs (re-rank, slice, brief, assign
lane, rule, comment, retire), propose-only with a human Apply for
anything authority marks Reto-only. The same turn runner serves the
MCP. This is where a stuck gripe is argued.

### 6. Dark lane: dispatch a slice to a lane

(two builds). The todo executor from `fleet-coordination-via-precis.md`
step 6, as the runner of the `run_slice` job type (Round 3): pick lane
(3), render brief (2), launch headless (`claude -p`, `codex exec`,
`scripts/devin-tree`, `scripts/kimi-tree`) in its own worktree or a
`sandbox_run` container, scoped token, push to its branch only; the
runner writes the handoff from the session's last message and the
branch state; a reviewer from another lane writes the verdict; W-tier
lanes land through `scripts/ship` like anyone else, under the branch
rules above (one branch per slice, review pinned to the tip, delete on
land). Harness sessions in tmux remain for Reto's desks and for a slice
marked `needs-human`. Mode B runners (MCP-TOKEN-FIX) take this lane
over once plan_tick proves it.

**Folded gripe gr541684 — same-file slices in one wave.** Wave 1 ran
td540842 and td540843 in parallel, both editing
`src/precis/todo/work_view.py`; the second lander hit a conflict the
seam notes could not prevent (~1 hand-resolved round — a guaranteed
cost, not a risk). The dispatcher's answer: each slice declares a file
set, and overlapping sets get a `blocked-by` link at mint, released
when the blocker lands — not refuse-at-dispatch, which would leave a
ready slice with no owner. The gripe's second ask, a sanctioned way
to wait on `scripts/ship`, is moot under land-immediately: the
builder stops at handoff, the lander owns the wait. **Open:** who
declares the file set — the slicer at mint (proposed) or the builder
in its first minute.

### 7. Alerts as slices — without shooting the foot

(reads 1b, 4, 6). An alert becomes a slice only through triage (infra
vs content, the `job_bubble` axis), deduped by fingerprint, capped per
fingerprint per day (the `diagnose_scan` cap pattern), and tagged
`factory-minted` so the work tree shows the auto-work volume on its own
line. Guards:

- **a fixer never touches the rule that fired.** Alert rules,
  thresholds, monitors and nursery detectors are in `deny_write_paths`
  for every lane; loosening a signal is Reto-only (already the "loosen
  a test" rule). An alert resolves only when its signal clears;
- **the factory never lands its own controller.** Dispatcher, release
  pass, lane table code, authority, alert rules = harness diff: full
  gate, human `/go`, never the dark lane. An agent may *propose* a
  change to them as a decision row;
- **a self-fix loop has a breaker.** N factory-minted slices from one
  fingerprint in a window, or a fix that re-fires the same alert, trip
  it: the fingerprint parks, a showstopper row is filed, and nothing
  else is minted for it until a human clears it;
- **one kill switch.** `service_config` prio 0 on the factory pass and
  `PRECIS_FACTORY=off`, the same switch that turned the fixer lane off
  in October; the arming ladder is permissive-dark → report-only →
  auto-slice with human review → auto-land content fixes → never
  harness.

### 8. Agent-level dashboard

(reads 1 + 3): per lane — sessions live, slices in build / review /
landing, quota remaining vs time to reset or deadline, pacing position,
first-round `LAND` rate by task class. Where "who is good for what"
lives, and where an idle lane beside expiring credits shows.

### 9. Inbound pull requests (Reto 2026-10-31, td543749)

Ruling: certified Dependabot PRs just land if they do not break the
pinned Python; every other PR gets an analysis and a human decision,
checking threats and the fit with the backlog and threads; a feature is
a design verdict. "Patch quickly but not idiotically." Ten Dependabot
PRs sat open on 2026-10-31, three of them major floor bumps.

- **Intake is a poll, the plan is `pr-intake`.** `pr_poll` (job_inproc,
  15 min, the public API) mints one plan per open PR, tagged `pr:<n>`.
  The class is decided in code, never by a prompt: `dependabot` —
  author `dependabot[bot]`, every commit GitHub-verified, files within
  `pyproject.toml` + `uv.lock`; `harness` — anything in `HARNESS_DIFF`
  or `.github/**` (Dependabot's actions bumps), an ask for Reto's `/go`
  and nothing else; `factory` — a known builder's PR whose title names
  its `(tdN)`: it is a slice, lander plus reviewer, no analysis;
  `external` — the rest. A fork is never `dependabot`.
- **Dependabot: resolve, land checked, review the changelog.**
  `dep_check` resolves the lock under python 3.12 and 3.13 in a
  throwaway worktree and never imports or runs the dependency; a major
  bump adds an `answer` gate (land / ignore this major / hold); the
  lander lands `slice/pr-<n>` with `scripts/ship --checked` — a
  pyproject diff selects the contract and import-map tests, the cheap
  insurance against a broken import reddening every later slice; the
  reviewer reads the changelog between the versions and the diff and
  writes LAND/FIX/REVERT; the land closes the PR with the squash sha.
  Security-linked bumps prio 2, routine prio 6.
- **External: read, never run, then ask.** The `analysis` slice fetches
  the PR head and reads the diff only — no `uv sync`, no tests, no
  executing a file from the PR before a human says land. It posts one
  `analysis:` comment: threats (secrets, network calls outside
  `safe_fetch`, workflow/deploy/vault/auth changes, new dependencies
  and typosquats, obfuscation, binaries), fit (which backlog item or
  `docs/backlog/threads/*`, overlap with in-flight trees, fix or
  feature), size. The ask is `approval` for a fix and `design-verdict`
  for a feature: land as is / land after fixes / request changes /
  close. Comments on the PR and closing it are outward-facing and run
  only after the answered ask (`pr_action` op).
- **Priority is the todo's prio**, across all roots: 1 release fixer,
  2 security bump or revert, 3 gripe fix, 4 feature slice, 6 routine
  bump, 8 PR analysis.

**How it breaks, and the guard:** a spoofed bot → login and signature
both, files-touched allowlist; a dependency that resolves but breaks at
import → `--checked` at land, the release gate for the rest, REVERT by
the reviewer or the release fixer; untrusted code executed on the
slice host → the analysis brief forbids execution and runs in the main
checkout, the deny list covers the harness, nothing runs before the
answer; a PR comment posted by mistake → `pr_action` only ever follows
an answered ask; the lander's gh identity is Reto's → gr543694 (the
attribution gap) covers PR actions too.

## Explicitly NOT in scope

- Moving backlog spec files into the database.
- A new scheduler for cluster jobs (`cluster-scheduling.md` stands).
- Reusing harness subscription tokens in a home-built loop (ruled out
  2026-10-09); headless runs use the harness itself.
- Auto-answering permission prompts; fewer prompts come from tiers.
- Letting a lane or a prompt bypass the ship gate, the release train or
  the authority file.
- Per-task token budgets.

## Acceptance (for the whole, checked per step)

- One call or page answers: what is in flight, who is on it, where the
  gripes sit, what blocks a lane, when the next build is. No tmux, no
  inflight JSON.
- A slice's next brief is regenerated from spec + last handoff; a fresh
  session completes a slice without a human re-explaining it.
- A dispatched slice lands on a free or subscription lane first; every
  windowed lane reaches its reset or deadline at ≥90 % used; metered
  spend only when those are full.
- A landed sha reaches prod without a human cutting the release; a red
  gate or failed probe stops the pass and shows as a showstopper.
- A factory-minted fix never edits an alert rule, a test ceiling or the
  factory's own modules.
- The quest board lists science quests and pillar quests in one ranking.

## Open questions

- **Q2 design.** The four flow-down questions listed in Round 3 (two
  quests, fairness, orphan roots, tick or trigger) need one design pass
  before step 5's build.
- **Agent SDK on a subscription.** Allowed or API-key only; decides
  whether Mode B runners use the SDK or shell out to `claude -p`.
- **Session key for headless runners.** The twin is keyed on
  `_ctx = {session_id, agent_id}`, set by a Claude Code hook. For a
  `claude -p` run the hook fires, but whether the id stays constant
  across the run and its subagents is unverified (MCP-TOKEN-FIX's hook
  logging answers it); Codex, Kimi and Devin have no hook and are known
  only by connection. The factory needs every tool call attributed to
  its slice, so the dispatcher passes the slice id in the launch env and
  the hook or router sets `_ctx` from it. Question: is that enough, or
  do non-Claude lanes need a per-slice bearer token?
- Kimi quota: may the reporter use the login token for
  `api.kimi.com/coding/v1/usages` (Reto)?
- Where the fit score lives: `lanes` meta, or a memory node per lane
  under me538174 so human notes and the machine score stay one record.
- Authority: a daemon as release owner (step 4).

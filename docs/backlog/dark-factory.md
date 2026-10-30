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
  (`precis-quest-help`). Asks: `ask-user:` todos, web `/asks` and the
  Needs-you landing (`routes/asks.py`); Reto's review queue
  (`.claude/fleet/review-protocol.md`).
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
and no detritus branches. What exists: `scripts/ship` already squashes
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
- **Review before land, from another lane.** The reviewer (lane table
  `exclude=` the builder's vendor) reads the branch diff against
  `origin/main` and the brief, and writes a `review` chunk on the slice:
  `LAND`, `FIX` (with the list) or `REJECT` (with why), pinned to the
  tip sha it read. `FIX` is a fix round on the same branch by the same
  lane; `REJECT` deletes the branch and requeues the slice with the
  verdict in its handoff. Reto reviews only what authority marks
  Reto-only (harness diffs, migrations, `safe_fetch.py`, authority,
  loosened tests); those land as decision rows in the asks list, with
  the diff handle, never in his inbox.
- **Landing is `scripts/ship`, run by the lander.** The lander is the
  release-owner daemon of step 4 (or a W-tier runner until the daemon
  exists). It lands a branch only when the newest `review` chunk says
  `LAND` *and* its sha equals the branch tip; a push after the review
  invalidates it. `--checked` by default, `--quick` for docs-only diffs,
  the squash message from the slice title and brief. A merge conflict on
  the forward merge is a fix round for the builder lane, not a human.
  After the land the lander writes the `landed` chunk and **deletes the
  branch and its worktree or container**, instead of ship's reset to
  `main`: the slice is done, so the branch has no owner left. Human
  trees keep the reset.
- **Deploy is the release pass** (step 4): previous release done, new
  shas on `main`, cut, gate, deploy pinned, close `awaiting-deploy`
  rows. Migrations and `safe_fetch.py` stay on the human `/go`.
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

**How it breaks, and the guard:** a builder with the lander's
credential → separate tokens, drift-tested like authority; a review
read on sha A landing sha B → the equality check above; reviewer and
builder from one vendor → `exclude=` is mandatory for the review role,
a missing lane table entry refuses the launch; the reaper deleting a
human's branch → it touches `slice/*` and stranded `*-pin` only, never
a branch with a live worktree or session; a `REJECT` loop → the
fingerprint breaker of step 7 (three rejects park the slice as a
showstopper). **Open:** whether to keep Devin on pull requests at all
or give it a branch credential like the others; the grace period for
stranded branches (three builds proposed); whether `--checked` is the
default even for prose-only diffs.

## Next steps, by unlock value

1. **One work tree, read-only first** (one build). `get(kind='todo',
   view='work')` + web `/work`: quest (pillar or science) → thread/spec
   (md frontmatter: `status`, `claimed-by`) → slice/gripe (loop queue)
   → builders (fleet rows per tree, count and vendor) → landed/deployed
   (round refs). Gripes hang where their `thread:` tag puts them,
   untagged ones in one bucket. Columns: status, agents on it, age in
   builds, the open release and its gate. Unlocks 2–8. Same build:
   install the fleet reporter so the tree has agents in it.

1b. **The asks list** (small, reads 1). One view and page with three
   classes — showstopper (a lane is stopped on it), decision (a next
   slice waits on it), discussion (a shape question) — grouped by quest,
   fed by `ask-user:` todos, `waiting-for:reto`, review-queue files,
   gripes tagged `needs:ruling`, red releases and tripped breakers.
   Replaces the review session's gather step. Argument = comments on the
   row; `all users see all` for now.

2. **Loop chunks on slices, not only gripes** (one to two builds).
   `edit(kind='todo', mode='fix', args={brief|handoff|review|landed})`
   with the same derived queue; a `brief` render = spec section (md kind,
   `~heading`) + the previous handoff + the landed diff's summary + the
   authority footer. A slice is one session: brief → build → handoff →
   exit; the next brief is generated, never carried in a session. The
   fix for "forgets its original prompt". The slicer role (frontier
   model) writes slices from a spec; the reviewer role writes verdicts;
   the mindmeld turn (Round 3) writes the `rulings` chunk, from Reto's
   numbered answers.

3. **Lane registry + pick** (one build, then learning). Table `lanes`
   per the capacity table above, seeded by hand from me538174; the
   nightly fit rollup; `exclude=` for cross-vendor review. Unlocks the
   agent dashboard and the self-improvement loop.

4. **Release pass, event-driven** (one build + one ruling). A worker
   pass: previous release done, new shas on main, no release open → cut
   `release/r<N>`, gate, deploy pinned, close `awaiting-deploy` rows.
   Judgment steps (verify the live product, `--confirm-runtime`) stay
   with a session until the dogfood verdict is mechanical.
   **The ruling it needs:** authority.md makes release and deploy
   Reto-only except the release owner; a daemon as release owner is a
   change to that file (Reto's commit). **How a home-built release loop
   breaks, and the guard for each:**
   - a red gate nobody reads → a red release is a showstopper row (1b)
     and the pass stops cutting until it is cleared, never retries;
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

5. **Pillars as quests** (half a build of data, one build for the tick
   type). Mint one quest per pillar, link each thread root `serves` it;
   the quest board shows science quests and pillar quests in one
   ranking with the same PRIO and logbook. The pillar's "where it
   stands" line becomes a logbook entry; `docs/roadmap.md` shrinks to a
   pointer and retires. The build tick (role set slice / build /
   review) is the quest-loop change; until it lands the pillar quests
   stay `dormant` and the board marks them as such, not as stalled.
   `web-quest-editor.md` and `quest-loop-cadence-strip.md` are the
   surface items this feeds.

5b. **List, click, chat** (one build, after 1). The design-workbench
   pattern on `/work`: rows are handles; a click selects, the chat form
   posts message plus handles; the turn runs with the fisheye of the
   selection plus the asks and lanes touching it; its ops are the loop
   verbs (re-rank, slice, brief, assign lane, rule, comment, retire),
   propose-only with a human Apply for anything authority marks
   Reto-only. The same turn runner serves the MCP. This is where a
   stuck gripe is argued.

6. **Dark lane: dispatch a slice to a lane** (two builds). The todo
   executor from `fleet-coordination-via-precis.md` step 6, as the
   runner of the `run_slice` job type (Round 3): pick lane
   (3), render brief (2), launch headless (`claude -p`, `codex exec`,
   `scripts/devin-tree`, `scripts/kimi-tree`) in its own worktree or a
   `sandbox_run` container, scoped token, push to its branch only; the
   runner writes the handoff from the session's last message and the
   branch state; a reviewer from another lane writes the verdict; W-tier
   lanes land through `scripts/ship` like anyone else, under the branch
   rules above (one branch per slice, review pinned to the tip, delete
   on land). Harness sessions
   in tmux remain for Reto's desks and for a slice marked `needs-human`.
   Mode B runners (MCP-TOKEN-FIX) take this lane over once plan_tick
   proves it.

7. **Alerts as slices — without shooting the foot** (reads 1b, 4, 6).
   An alert becomes a slice only through triage (infra vs content, the
   `job_bubble` axis), deduped by fingerprint, capped per fingerprint per
   day (the `diagnose_scan` cap pattern), and tagged `factory-minted` so
   the work tree shows the auto-work volume on its own line. Guards:
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

8. **Agent-level dashboard** (reads 1 + 3): per lane — sessions live,
   slices in build / review / landing, quota remaining vs time to reset
   or deadline, pacing position, first-round `LAND` rate by task class.
   Where "who is good for what" lives, and where an idle lane beside
   expiring credits shows.

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

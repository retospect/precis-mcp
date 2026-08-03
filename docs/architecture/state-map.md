# Current-state map — discovery / task / worker / review subsystems

> **On-demand detail for `CLAUDE.md`.** This is the present-tense map a
> session reads *before touching* one of these subsystems — moved out of
> `CLAUDE.md` so the always-loaded brief stays lean. `CLAUDE.md`'s
> "Subsystem map" section is the index into the anchors below.
>
> It is **present-tense** — for the dated story of how each piece landed,
> read the **git history** (`git log`); there is no CHANGELOG. **Keep this
> file true: update it in the same commit that changes what it describes**
> (the rule that used to apply to `CLAUDE.md`'s body now lives here).
>
> The `precis-*-help` skills are the authoritative, on-demand reference for
> each kind (`get(kind='skill', id=…)`); the per-affordance lines below are
> the index + the "where's the code" pointer. Unfamiliar coined or overloaded
> vocabulary (`tote`, `bubble`, `dark`, `tier`, `card`, `dispatch`, …) →
> `docs/architecture/glossary.md` (term → best entry-point file).

## The todo tree (five slices)

`kind='todo'` is a hierarchical task graph unifying intent,
scheduling, execution, and review.

**The facet model (§M, migration 0102).** A todo is **one faceted
kind** — `tags` + `meta` — not a family of kinds. The next "type" of
work is a field or a tag on `kind='todo'`, never a new kind: the level
gradient, the schedule/recurring shape, and the LLM auto-run tier are
all `meta` fields (below), not sibling kinds. The **one** boundary that
stays is todo ↔ job (ADR 0030, defended on physical grounds: a job is
claimed/leased/executor-run — `FOR UPDATE SKIP LOCKED`, `idem_key`,
the sweeper, lease-steal, reserve-at-claim slots — a todo is durable
intent and is never leased; merging would force row-lock contention or
two state machines onto one ref). See `tests/test_todo_job_boundary.py`
for the durable pin of that boundary.

* **Hierarchy.** `parent_id` column on refs; a strategic / tactical /
  subtask gradient — `meta.rotation_root=true` (strategic root) /
  `meta.worker_mintable=false` (tactical) / neither set (subtask, the
  worker-mintable default) — with walk-on-read ancestry and a 1/N
  rotation across strategics by 7-day picks. Reparenting goes through
  a reserved `parent` **link** relation (ADR 0027), not a raw column
  write.
* **`meta.auto_check` leaves.** Wait-for-condition evaluators under
  `auto_check_evaluators/`: `paper_ingested`, `discord_reply_received`,
  `time_past`, `tag_present`, `child_job_succeeded`.
* **Recurring ("Watches" umbrella).** `meta.schedule` (cron /
  `every:` shorthand for recurring, or a one-shot `at` timestamp — ADR
  0061) presence *is* "recurring" — drives a per-minute spawner. No
  separate tag: `level:recurring` was redundant with `meta.schedule`
  and is retired (§M). Queue-mode ticks mint an ordinary
  worker-mintable subtask child (the original Slice-4 behaviour); a
  recurring carrying `meta.deliver={'target': ...}` instead fires a push
  notification (`pg_notify('precis.cron', ...)`) asa_bot turns into a
  synthetic prompt — this is the retired `kind='cron'` mechanism,
  folded on by ADR 0061 (superseding ADR 0030's cron ruling). `PRIO`
  is an int column on refs (1..10); `PRIO:*` tag stays as a
  back-compat alias.
* **Jobs hang off an owner ref (parent-kind polymorphic, ADR 0044).**
  `JobHandler.put` requires a `parent_id`, but that parent is one of two
  lanes, distinguished by its **kind** (`JOB_PARENT_KINDS`), not a
  declared flag. **Intent lane** — parent is a `kind='todo'`: the classic
  case (rotation + the `child-failed` bubble + `child_job_succeeded`).
  **Compute lane** — parent is a build subject (`structure`/`cad`/
  `draft`): a *derived* job (DFT relax / route / compile) — idempotent,
  content-addressed, cache-fillable — owned by the artifact, which has no
  rotation to enter. An intentful task that wants to *block* on a derived
  build links `requested`→job (migration 0046); `derived_job_succeeded`
  closes the requester on success and the failure-bubble follows the link
  on failure. This dropped ADR 0043's "relax needs a parent todo". The
  `dispatch` worker
  walks open todos carrying `meta.executor`, mints `kind='job'` under
  each with `FOR UPDATE SKIP LOCKED`, and auto-injects
  `meta.auto_check={'type':'child_job_succeeded'}`. On job failure
  the parent gets a `child-failed:<job_id>` open tag (the
  failure-bubble, `handlers/_job_bubble.py`); the doable view excludes
  bubbled parents so they stop re-entering the rotation until the
  owner decides retry / switch / give up. **Infra vs content (fix for the
  07-26 latch):** the bubble classifies by the child's own tags —
  `INFRA_FAILURE_TAGS` (`swept:claim-orphaned`, the sweeper's lease-expiry
  class) → a *bounded* auto-retry: the parent is **not** latched (stays a
  candidate, re-mints a fresh child — safe since the todo-lane mint has no
  idem_key and the swept child's lease is already dead), capped at
  `ORPHAN_RETRY_CAP` (3) per `ORPHAN_RETRY_WINDOW_HOURS` (6h,
  `meta.orphan_retry_count`/`_window_start`), past which it latches
  `child-failed:` + `halt:orphan-retry-cap`. A content-class failure (no
  infra tag) latches `child-failed:` immediately, unchanged — so a transient
  orphan self-heals in a cycle instead of parking dark for days.
* **Planner coroutines.** A `meta.llm_tier`-set todo (§M — demoted off
  the old `LLM:*` tag; the value picks the capability tier) runs the
  `plan_tick` coroutine — each tick is a `kind='job'` that may mint
  children (`verdict: continue`) or yield (`ask-user:`) and still exit
  `STATUS:succeeded`. `child_job_succeeded` is guarded so it never
  auto-closes a parent with `meta.llm_tier` set or still has a live
  child todo, and `dispatch` strips the spec when minting a
  self-resolving tick. Job lease is 90 min (covers a 60-min tick plus
  post-processing). A tick cut off by an **exhaustion** — the
  `--max-turns` ceiling *or* the wall-clock timeout (exit 124) — is
  **resumable, not a failure**: the executor (`_resume_reason`) marks it
  succeeded-but-non-blocking so `dispatch` re-mints a fresh tick, bounded
  by a per-parent streak cap (`meta.plan_tick_resume_streak`, default 3,
  env `PRECIS_PLAN_TICK_RESUME_CAP`) past which — gripe 168886 tier 2 —
  `claude_inproc` auto-mints one narrowly-scoped `plan_tick` follow-up in
  `decompose` mode (forcing prompt: split into 2-5 subtasks, don't attempt
  the task) instead of bubbling immediately, guarded by a permanent
  per-parent `meta.plan_tick_decompose_attempted` flag; a *second*
  streak-exhaustion after that bubbles for real as `child-failed:<job_id>`
  (the task needs splitting further than the auto-decompose could manage).
  A live child todo only blocks
  re-candidacy unconditionally when it's a genuine in-flight child or
  carries a hard-block tag (`halt`/`halt:`/`child-failed:`); a child
  parked on `ask-user:`/`waiting-for:` alone (no hard-block tag also
  present) stops blocking once 6h have passed since the parent's last
  `plan_tick` job (`dispatch._parked_child_still_blocks_sql`,
  `_todo_views._replan_bypass_clause`/`_hard_block_clause`) — so one
  human-blocked leaf no longer freezes the whole planner subtree; the
  re-ticked planner is prompted (`planner_prompt.py`, "Re-ticked while
  a sibling is parked") to propose autonomous next steps or escalate to
  `halt:` if the pending answer has become a genuine hard gate.
* **Views.** `view='tree'` walks `kind IN ('todo','job')` so child
  jobs render with a `⚙` marker; `view='attention'` unions
  `asking-reto` leaves + `child-failed` parents for asa-bot's preamble;
  `view='projects'` (`_todo_views.render_projects`) is a dashboard of
  workspace-owning roots. View dispatch in `handlers/todo.py` is a
  `TodoView` StrEnum + `_TREE_SEARCH_VIEWS` table with an import-time
  totality assert.
* **Projects.** A *project* is a strategic-root todo that owns a
  `meta.workspace` (no new kind). `TodoHandler.put` stamps a
  `project:<slug>` owner-path tag derived from `meta.workspace.path`
  (`utils/workspace.project_tag_for_path`), even on operator/CLI
  writes (not just planner ticks). A first-class `Workspace.brief`
  (`meta.workspace.brief`) cascades down the subtree and is injected
  as a `## Project context` block into the planner prompt's *variable*
  layer (`workers/planner_prompt._render_project_brief`) — kept out of
  the cached system layer since it is per-project. Skill:
  `precis-tasks-help` (Projects section).

## Review tiers

Three reviewers write memory digests, factored into
`workers/review.py` (`Reviewer` dataclass + `run_review_pass`
driver; adding one is a `Reviewer(...)` instance):

**Empty-result assertion** (driver-level, all agentic reviewers). Before
`run_review_pass` writes a success digest it checks `_is_silent_empty(res)`:
the conjunction **cost∈{0,None} ∧ turns∈{0,None} ∧ `tool_calls`==0 (definitive)
∧ no text** means the pass returned but did nothing — a silent failure on a
*capable* host (the §capability-probe already diverts incapable ones). It writes
a failure marker (backoff) and raises a per-reviewer `warn` alert
(`review:empty:<name>`) instead of a $0 "success" digest; a later real digest
resolves it. `tool_calls` is counted from `tool_use` blocks in the stream-json
stream (`claude_agent._count_tool_use_events` → `AgentResult.tool_calls` →
`LlmResult.tool_calls`); it is `None` on the text/stderr path, so a definitive
`0` (positive evidence of zero tool calls) is required to trip the guard — a
cheap-but-real pass is never flagged.

* `nursery` — SQL-only, every minute on the system worker. Flags
  orphans, stale claims, long waits, stuck doable, stalled recurrings,
  **spin loops** (any `(ref_id, source)` emitting >
  `SPIN_LOOP_EVENTS_24H` (200) `ref_events` in 24h), **plan-tick
  spins** (a planner parent minting > `PLAN_TICK_REMINT_24H` (16)
  `plan_tick` jobs in 24h — the coroutine "succeeds" each tick but never
  converges, which the resume-streak cap doesn't catch since it only
  guards exhaustion loops), **child-failed-parked** (gripe 168886 tier 1
  — a todo carrying an open `child-failed:*` tag past
  `CHILD_FAILED_PARKED_HOURS` (6h), the blind spot `stuck-doable` misses
  since it excludes anything with open tags), **orphaned-coordinator**
  (`_detect_orphaned_coordinator`, `critical` — an active quest_tick or
  planner coordinator whose *newest* job is terminal-`failed` and stale >
  `ORPHANED_COORDINATOR_STALE_HOURS` (6h) with no live/queued replacement:
  the "reconciler host down → zero re-mints" blind spot `_detect_quest_loop_
  failures` structurally can't see, since it's one failed row not a spin —
  the 4-day silent outage 2026-07-26→30), and **worker health** (daemon liveness, not
  the todo graph): **worker-restart** (a `(host, process)` emitting >
  `WORKER_RESTART_STORM_1H` (8) `worker: started` boot rows in 1h — the
  jetsam-cull signature that was invisible for 1.5 days; the boot row is
  emitted at `cli/worker.run` startup, the only DB restart signal there
  is) and **dead-worker** (a continuous daemon in
  `WORKER_CONTINUOUS_PROCESSES` silent > `DEAD_WORKER_SILENCE_MIN` (10)
  min while its host is otherwise alive) and **dispatch-stall**
  (`claude_inproc` jobs sitting `STATUS:queued` > `DISPATCH_STALL_MINUTES`
  (15) with **zero** live-lease jobs running — the single agent-profile
  executor stopped claiming: culled / OAuth-401 / never-started. Minting is
  cluster-wide but execution is melchior-only, so this is the "45 min dark"
  SPOF, gripe 55748. The "nothing running" gate distinguishes a dead
  executor from a healthy-but-backlogged one; symptom-level, so it also
  catches an agent worker that never started — which has no log rows for
  dead-worker to age). These three, plus `orphaned-coordinator`, the
  host-level **nas-denied** (`_detect_nas_denied` — a host's fresh
  `host_heartbeat` reports `/opt/nas` unreadable from the reporter's own
  launchd context; the FDA-grant-broke-on-brew-python-bump lockout), and
  **host-dark** (`_detect_host_dark`, gr186752, §D — a host's OWN
  `host_heartbeat` row itself gone stale, bounded to hosts with recent
  `worker_logs` activity so a decommissioned host ages out: since §A/§L the
  heartbeat pass runs *inside* the per-host worker it reports on, so a dead
  single-writer host takes its own heartbeat down with it and drops out of
  `dead-worker`'s `host_alive` gate — `dead-worker` self-suppresses there by
  design, one dead host must not fan out into N per-daemon alerts, and
  `host-dark` is the deliberate complement that still raises exactly one
  critical for it), are the `critical` categories — a thrashing/dead/stalled
  worker stalls the planner
  cluster-wide, so on the *first* sighting `raise_alert` (now returning
  `(ref_id, is_new)`) fires a one-shot `notify_critical_alert` — a
  `kind='message'` to `PRECIS_OPS_ALERT_TARGET`
  (`discord/<guild>/<channel>`, the same asa_bot channel the daily news
  briefing uses; no webhooks exist in this deployment) via
  `pg_notify('precis.messages')`; default unset → the push merges dark;
  alerts still land in `/alerts` + agent triage. Each
  finding is raised as a `kind='alert'` (one per condition, `alert_source
  = nursery:<category>`, deduped on `meta.fingerprint`; a non-ref-scoped
  worker-health finding sets `ref_id=None` + an explicit
  `fingerprint_key`; cleared conditions auto-resolve) — **not** a
  `kind='memory'` digest any more. See `Other live affordances` →
  `alert`, and `precis-nursery-help`. (Replacing the digest killed a self-spin: the
  spin-loop finding set churns every second, so the old
  `(category, ref_id)` digest fingerprint changed every pass and the
  per-node per-minute writer emitted >2000 near-dup memories/day.)
* `health_digest` (§D, `docs/proposals/health-watchdog.md`, Phase 1) — the
  slow-rot sibling of nursery: SQL-only, no LLM, fired hourly via the
  `health_digest` **scheduler-lease cadence** (`workers/scheduler.py`
  CADENCES, host-agnostic — any live worker can win it; §A's lease
  machinery IS the fleet-singleton throttle, no separate `app_state`
  throttle invented). Three check sources: **curated Layer-1** outcome
  checks (~13, budgets from the design doc's pulse-probe — papers/chunks/
  news/casts/taproot/agent-jobs/hosts/alert-backlog; the two with a
  natural backlog — `embed`/`chunk_keywords` — are **idle-aware** via
  `precis.health_checks.compute_backlog_counts`, the same computation
  `/status` renders, so an empty backlog reads `ok` and only a
  non-draining one past budget reads `stale`; `chunks_extracted` is
  body-row-only (`ord >= 0`) and input-aware, stale only when a paper
  landed past budget newer than the newest body chunk, so a card_forge
  rewrite can't mask a stalled extraction pipeline. No curated row for
  `dream_agent`/`anki_sync` — a fixed budget would contradict the derived
  cadence-staleness lane against a live-resolved interval); **cadence
  staleness** (derived, every `scheduler_leases` row overdue past
  `interval_s + margin`, zero per-cadence config — this is `dream_agent` /
  `anki_sync`'s only check); **Layer-2 coherence** (derived, every
  registered `PASS`+`ref_pass` `ServiceSpec` that resolves enabled —
  structural default or a live `service_config` override — with zero
  `worker_logs` in 24h reads "intended-on but silent", straight off the
  registry so a new pass needs zero digest edits). Findings raise
  `kind='alert'` under `alert_source="watchdog:<group>"` (severity capped
  to info/warn — nursery keeps `critical`), auto-resolving via the same
  `resolve_stale_alerts` sweep nursery uses. Push policy: a templated
  (zero-LLM) `kind='message'` digest to `PRECIS_OPS_ALERT_TARGET` on the
  daily heartbeat (`app_settings['health_digest:last_push']` > 24h — an
  all-green push IS the dead-man's proof the watchdog is alive) or a
  fresh degradation. After every eval, an external dead-man's-switch ping
  (`PRECIS_DEADMAN_PING_URL`, via `safe_fetch.safe_get`) covers the one
  case nothing DB-mediated can — a total fleet/DB outage; see
  `docs/runbooks/dead-mans-switch.md`. `ServiceSpec` carries `ref_pass=True`
  with no `default_profiles` (mirrors `dream_agent` — the manual `--only
  health_digest` registration is separate from the cadence's standing
  trigger). See `precis-health-digest-help`.
* `structural` — opus, 6h dedup, agent profile. Drift, sibling
  contradictions, depth/fanout warnings. Dedup is symmetric: a **failed**
  dispatch (non-paused error — e.g. the agent container missing
  `PRECIS_DATABASE_URL` on a host) writes a `review-fail:<name>` cooldown
  marker so the pass backs off to `min_interval_hours` instead of
  re-dispatching every tick (was spinning spark to 124k ERROR/24h).
* `deep_review` — opus, weekly dedup, agent profile. Allen-style
  archive / prune / rebalance / long-wait review.

**Explicit tier-1 tool-deny for the standing passes (gr179501).** These
passes never set an `meta.envelope`, so the envelope defaults permissive —
they now pass an explicit `disallowed_tools` to the router instead of
trusting the prompt footer. Reviewers (`_REVIEWER_DISALLOWED_TOOLS`,
`review.py`) deny mutate-existing verbs + fs-write + shell + web, keeping
`mcp__precis__put` for the `_footer_block` gripe carve-out; `dream_agent`
(`_DREAM_DISALLOWED_TOOLS`) denies edit/delete/link + web but keeps
`put` (new memory) and `tag` (its Step-7 `tier:synthetic-insight`
promotion — a needed cooperative-tier residual the tool-level deny can't
scope by kind), since its fisheye pulls unvetted paper/patent summaries
into the prompt.

## Workers

**Service registry (the declarative source of truth).** Profile
membership + the extra `PRECIS_*_ENABLED` gates are declared once in
`src/precis/workers/registry.py` — a frozen `ServiceSpec` row per
pass/job-type/compute/daemon/serving (factory-console slice 1,
`docs/design/factory-console-and-scheduling.md`). `cli/worker.py`
derives `system_passes`/`agent_passes` via `service_names_for_profile()`;
`spec.enable_env` now (§L, below) only seeds the deploy-time
`service_config` row, not a live boot/gate read. The `/env` inspector derives its agent list
from the rows carrying an `AgentIntrospect` (the old `AgentSpec` tuple is
gone). **Assembled context (Part 3B, `precis_web/env_context.py`):** `/env`
now also renders the FULL assembled prompt input (ADR 0038) per agent —
"last real" (the most recent `meta.assembled_context` capture, wherever
Part 3A's `persist_assembled_context` landed it: the plan_tick job ref for
`job_claude_inproc`, the digest memory for `structural`/`deep_review`) and
"dry-run" (a fresh zero-LLM-call preview via `build_planner_prompts` /
`_assemble_reviewer_blocks`); dream degrades to a "hand-rolled, not
captured" note (no assembler on its path). The `/env` view is the live
companion to the **context-quality audit** — `docs/design/context-quality-eval.md`
(durable catalog of every context precis assembles + the 6-dimension
inspection rubric) and its Sonnet-runnable sampler under
`scripts/context-audit/`. `tests/test_worker_registry.py`
AST-parses `cli/worker.py` and
fails CI if a wired pass has no spec (or a `ref_pass=True` spec has no
wiring site), so the four parallel lists can no longer drift.

**Live run control — `service_config` (slice 2, §L cutover).**
`service_config(host, service, prio, model_pref, write_level, …)`
(migration 0072) is the ONE control surface for what a worker runs —
`prio 0` = off, `1..10` = claim weight (fed into the scarcity+prio+age
claim ordering slice 6 adds). `workers/service_config.py::ServiceConfigResolver`
(a short-TTL cache) is read *only* per-cycle (`run_loop`'s `pass_gate`) —
**registration never reads the DB.** §L (`docs/proposals/cluster-scheduling.md`
Pillar 1 §L) generalized what used to be a categorizer-only exception
(`classify`/`classify_topics`/every `axis:<id>`, registered unconditionally
so a live On-flip actually started them) to EVERY named pass:
`cli/worker.py::_should_register` (bound per-invocation as `_register`) is
purely structural — a pass registers when it belongs to the running
`--profile` rotation, when its `ServiceSpec.enable_env` is set (a
formerly-env-gated pass), when its name starts with `axis:`, or when the
core registry has never heard of it at all (a `precis.ref_passes` plugin
factory's own pass name — it already gated its own eligibility). `--only X`
still forces exactly one pass. The per-cycle gate's no-row baseline
(`_gate_default_on`/`_profile_default_on`) is `name in profile_passes` —
**`PRECIS_*_ENABLED` is retired as a live default**: a formerly-env-gated
pass (classify/hub_refine/llm_summarize/paper_glossary/…) with no row now
defaults OFF outright, not "whatever the env said." Two narrower exceptions
keep their own env-seeded per-item default, deliberately out of this
cutover's scope (granular deploy-time convenience seeds already fully
live-controllable per-item via their own row, not a whole-pass boot flag):
`axis:<id>` from `PRECIS_AXES_ENABLED`, `topic:<slug>` from
`PRECIS_TOPICS_ENABLED`/`PRECIS_CLASSIFY_TOPICS_ENABLED` (ADR 0068).
Closes the GAP the old design left: since `_pass_enabled` used to consult
the resolver at boot too, a stale/absent row at boot could keep a pass out
of `ref_passes` forever — a later live prio flip (either direction) had
nothing to gate until a restart (the `/categorizers` "activated but nothing
happens" bug, now generalized-fixed for every pass, not just categorizers).
CLI: `precis service prio|model|clear|list|seed` — `seed` (§L) is the
deploy-time, `INSERT … ON CONFLICT DO NOTHING` sibling of `prio`: the
`precis_worker`/`precis_worker_agent` roles run it before stripping a
retiring `PRECIS_*_ENABLED` plist flag, mirroring today's live state into a
row so the cutover is behaviour-preserving and a console override set
after first-seed survives every later redeploy (`prio` intentionally
UPSERTs and must never be used for this).

**§L hardening quartet.** Four smaller fixes landed alongside the control
cutover:

- **Heartbeat timer fully retired.** The standalone `precis_heartbeat`
  role/playbook are deleted and `redeploy-precis.yml` no longer re-renders
  `com.precis.heartbeat.plist` (which carried an inline `agent_rw` password —
  gr171431); `retire-thin-timers.yml` boots the old launchd/systemd timers
  out. Per-host heartbeat is the §A `heartbeat` worker pass
  (`workers/heartbeat.py`); the capability-probe host-var overrides the old
  plist carried (`precis_local_llm_model_override` / `precis_local_serve_config`)
  already ride the worker plists.

- **OS-agnostic manage (gr180078).** `scripts/restart-worker-and-watch`
  detects launchd vs systemd (`command -v launchctl`/`systemctl`) and picks
  the matching verb (`launchctl kickstart -k` / `systemctl restart`) —
  see `docs/runbooks/restart-worker-and-watch.md`. The nursery's
  `dead-worker` alert (`workers/nursery.py::_dead_worker_detail`) tailors
  its suggested recovery command the same way, reading `platform` off the
  host's freshest `host_heartbeat.meta` (mirrors the pre-existing
  `worker-restart` detector's `_restart_storm_detail` hedge).
- **Password out of plists (gr171431 §1).** Every `deploy`-run daemon
  (worker, worker-agent, web, watch, extract_watch) and both `hermes`-run
  ones (asa_bot, asa_slack) now render a password-free
  `postgresql://agent_rw@host:port/db` DSN; libpq resolves the password
  from `~/.pgpass` (the `pgpass` role, extended to also drop
  `~hermes/.pgpass` on the gateway). The two Linux/systemd units whose
  `HOME` diverges from the pgpass role's Linux deploy home set
  `PGPASSFILE` explicitly. Deferred, documented in-template: asa's
  `ACATOME_PG_PASSWORD` / `PRECIS_NOTIFY_DATABASE_URL` (a distinct
  direct-tunnel host:port) and `extract_watch`'s `ACATOME_PG_PASSWORD` — a
  raw-password contract a third-party tool's own config reads directly,
  not a libpq DSN pgpass can intercept.
- **Bounce coverage (gr176337).** The four remaining raw
  `bootout`→`bootstrap` sites (`precis_heartbeat`, `precis_watch`,
  `precis_embedder`, `precis_embedder_watchdog` — each ran on EVERY
  role invocation, not just a plist change) are now the same idempotent
  `launchctl load -w` no-op `precis_worker` already used (gripe #48481);
  the real reload-with-new-env fires only via each role's `notify`
  handler, already routed through the shared poll-until-gone
  `deploy/tasks/reload_launchd.yml`.
- **Teardown subprocess reap (gr171254).** `precis watch`'s batch-subprocess
  pool (`cli/watch.py`) tracks every spawned process-group id in
  `_inflight_pgids`; `SIGTERM`/`SIGINT` (and `atexit`, belt-and-suspenders)
  now call `reap_tracked_process_groups()` — TERM, a bounded grace, then
  KILL — instead of passively waiting for the in-flight batch's own
  `proc.wait()` to return. Without it, a deploy bounce whose kill-grace
  outlasts an in-flight Marker batch SIGKILLs the watcher before its own
  `finally: killpg` runs, orphaning the batch's detached surya server.
- **Live-path Marker hang guard (improvement-plan P2-3).** The live
  watchdog-event ingest runs Marker in a killable spawn-child
  (`ingest/marker.py::_marker_extract_subprocess`) with a wall-clock
  timeout — `precis watch --marker-timeout-s`, default 900, `0` disables;
  on expiry the child is killed and the fitz fallback takes over. Batch
  backfill children stay in-process (ADR 0015 rejected per-PDF model
  reload there); instead the parent bounds each batch child's wait at
  `len(batch) × timeout` and continues on expiry (ADR 0014 lock-file
  recovery owns the in-flight PDF). A wedged torch forward pass never
  returns to the interpreter, so a killable child is the only real guard.

**Bounded in-pass concurrency — `service_config.concurrency` (migration
0091).** A second live knob on the same table, resolved the same way as
`prio` (`ServiceConfigResolver.concurrency`, exact-host-over-`*`, TTL-cached;
`set_service_concurrency`/`list_service_config` on the write/inspect side).
It's the thread-pool width a cloud-calling categorizer pass fans its per-row
LLM cascade across — `classify` (ADR 0047 role3/junk) claims a batch then
makes 2-3 blocking round-trips per chunk *serially*, almost entirely
network-idle time; `workers/classify.py::run_classify_pass(concurrency=)`
runs that cascade (`_classify_row`, DRY-lifted out of the old loop) over a
`concurrent.futures.ThreadPoolExecutor`, while the claim/enrich/tag-write DB
work stays single-threaded on the main thread. NULL/no row (default `1`) is
byte-identical to the old always-serial pass. Clamped at a hard ceiling
(`PRECIS_CLASSIFY_MAX_CONCURRENCY`, default 32) inside the worker regardless
of what the knob asks for, so a fat-fingered console value can't stampede
the cloud endpoint. `cli/worker.py`'s classify wiring reads the resolved
value per cycle (same place the enable gate is read) and passes it through;
`/categorizers` exposes a small number input alongside each row's On/Off/
Default toggle (`POST /categorizers/concurrency`) — wired generically for
every categorizer row, though only `classify` consumes it today.

**Console — merged into System's Services tab.** The read-only host
strip (`host_heartbeat` load + liveness) over one list per category of
every registry service, joined to its live `service_config` prio and its
last-ok/last-fail from `worker_logs` (keyed by the `BatchResult.handler`
string via `ServiceSpec.log_handler`), now renders at
`/status?tab=services` (`src/precis_web/routes/status.py::index`,
`_services_ctx`). The live-edit prio/model_pref writes stay at their
original paths (`POST /factory/prio` / `/factory/model` / `/factory/clear`,
`src/precis_web/routes/factory.py::set_prio`) — only `GET /factory`
retired to a redirect; see "Web UI" below for the full merged-surface
story. Cloud-LLM placement is operator-owned per-tier chains (ADR 0066):
`POST /factory/llm/chain` writes one `llm.chain.<tier>` JSON list and
`POST /factory/llm/cloud` flips the `llm.cloud_enabled` throttle
(`status.py::_llm_chain_ctx`, `factory.py::set_llm_chain`/`set_llm_cloud`).
The old fleet-wide global backend flip (`POST /factory/llm`,
`GLM_OPENROUTER_PRESET`) was retired in Phase C — it was gripe-171782's
footgun (it dragged SMALL's `summarizer` alias to OpenRouter → 400); a
chain rung pins a concrete valid slug + its own transport, so it reaches
OpenRouter without touching the other tiers.

**Capability universalization (slice 5).** The *incidental* kind gates —
a raw-cache dir any host can create, edgar's descriptive User-Agent
string — are dropped from `KindSpec.requires_env` and defaulted via
`precis.config` (`cache_root`/`patent_raw_root`/`edgar_raw_root`/
`edgar_user_agent`). So `edgar` is available on every host and `patent`
gates only on the genuinely-scarce EPO credentials (`requires_secret`,
via the vault) + the `epo_ops` dep probe — the honest "Kinds unavailable"
set shrinks to the physical/real. (`python` stays gated: exposing local
filesystem roots is a deliberate scoping choice, not incidental.)

**OAuth materializer → vault (slice 0, code).** Both `ensure_oauth_token`
mirrors (`utils/claude_oauth.py` + `asa_bot/oauth.py`) source the
long-lived `CLAUDE_CODE_OAUTH_TOKEN` from the DB secrets vault when no
`~/.claude_oauth_token` file is present (asa over its existing
`PRECIS_DATABASE_URL`), so agentic daemons can run as `deploy` with no
`~/.claude` state — de-pinning agentic work from the hermes principal.
Ships safe (vault is a *fallback*). The live cutover — seed the vault,
verify, flip run-as to `deploy`, scope vault read, retire hermes — is an
ordered ops sequence (docs/design/factory-console-and-scheduling.md §12).

**Resource substrate — `resource_slots` (slice 6b, dark).** `resource_slots
(host, resource, capacity, free, kind)` (migration 0073) is the per-host
capability + slot map. The `heartbeat` reporter self-probes what each
machine can do (`workers/capability_probe.py`: `gpu` via `nvidia-smi -L`,
`podman`/`tts` via `which`/`find_spec`, env overrides
`PRECIS_{GPU_COUNT,PODMAN_SLOTS,TTS_SLOTS}`) — vocabulary *derived from*
`ServiceSpec.requires`, so present→advertise, absent→retract,
unknown→leave-the-row (a transient probe hiccup never retracts a real
capability, nor — once 6c lands — drops a live reservation). It syncs the
verdict each cycle (`store.sync_host_resource_slots`, best-effort) and
`/factory` renders each host's slots as chips. **Populated but unconsumed**
— `free` always equals `capacity`; scheduling is unchanged until slice 6c
reserves at claim (materialized-counter decrement in the claim txn, release
on terminal + the existing `meta.lease_until` sweeper — no separate lease
table). The soft memory signals are an unbuilt 6-sub-slice.

**Reserve-at-claim (slice 6c, dark).** A job declaring `meta.requires`
(`{resource: units}`) reserves those slots on the claiming host inside the
claim txn — `reserve_resource_slots` (all-or-nothing conditional decrement,
the lock itself) stamps `meta.reserved`; an unservable job is dropped from
the batch and waits for a host with capacity. `release_job_reservation`
refunds at terminal (`set_status`) and on crash recovery (the sweeper,
which writes `STATUS:failed` directly) — idempotent + capped. No prod job
carries `requires` yet, so nothing reserves until 6d wires the compute
job-types; the mechanism is inert until opt-in.

**6d — activation + self-gating (partial, unshipped).** `effective_requires`
derives a job's needs from its `job_type` ServiceSpec (`struct_relax`/`fold`
→ `{gpu:1}`); the claim reserves on `target_node`-or-local and *self-gates*
— only a resource that host advertises is reserved, an unadvertised one
falls back to the node-gate pin (no deploy stall). The sweeper flags a
queued job needing an unadvertised capability with no pin
(`_alert_unschedulable_jobs` → `scheduler` alert source). Deferred:
capability-rarity ordering + soft memory signals. `target_node` stays (node
gate + cache-affinity hint), not retired.

**Claim ordering — prio+age (slice 6a).** `claim_executor_jobs`
(`workers/executors/_common.py`) orders `COALESCE(prio, 5) ASC, ref_id
ASC` (was pure `ORDER BY ref_id` FIFO) — LOWER prio claims first, the
`0014_refs_prio.sql` convention every prio writer follows (prio=1
chat/preempt, 2 cron, 5 default/NULL) — and `dispatch` mints each child
job with `prio = <parent todo's prio>` — so prio flows down the DAG and
an urgent (low-number) quest/project claims its compute ahead of
commodity work, oldest-first within a band. An all-unset queue is
byte-identical to the old FIFO. The capability-rarity term (§5.3, 6d) is
not yet added.

**Boot-epoch lease + generalized crash recovery (§H cycle c,
`docs/proposals/compute-lane-lease-epoch.md`, built).** Every worker
process mints a `worker_boot_id` (uuid4) at startup
(`workers/heartbeat.py::mint_boot_id`) and advertises it in
`host_heartbeat.meta.boot_ids: {process: boot_id}` (nested-merged per
process on write, no migration — meta is JSONB, so a host running both
profiles never clobbers the other's generation). `claim_executor_jobs`
(`workers/executors/_common.py`) stamps `meta.lease_boot_id` /
`lease_process` / `lease_host` on EVERY claim, fresh or reclaimed, for
every executor uniformly. With `reclaim_stale_running=True` (now
`ssh_node`, `claude_inproc`, AND `claude_docker` — was `ssh_node`-only
pre-§H-c) a `STATUS:running` row is claimable via EITHER of two arms:
**expiry** (unchanged — lease provably past) or **epoch** (the row's
stamped `lease_boot_id` no longer matches the CURRENT advertised
generation for its `(lease_host, lease_process)` — the holder was
provably replaced, e.g. a deploy bounce — reclaimable even while
`lease_until` is still hours away). A live holder (boot_id still
matches) is never stolen by either arm, regardless of lease age.

**Generalized attempt cap, epoch-aware (`poison_guard`).** The bump +
classification happens once, in `claim_executor_jobs`; each executor's
own claim wrapper then calls the shared `poison_guard` on every claimed
row. Critically: **only an *expiry*-reason reclaim bumps
`meta.attempts`** — an *epoch*-reason reclaim (redeploy churn, not a
crash-loop) never does, so a deploy bounce mid-run can't burn the
crash-loop poison guard the way it used to. Every reclaim (either reason)
appends a capped forensic `meta.reclaims: [{at, why}]` entry. Past
`PRECIS_MAX_JOB_ATTEMPTS` (default 3) EXPIRY reclaims, the row is failed
+ bubbled (`failure_class='infra'`) instead of run again. A stolen job's
stale `meta.reserved` slots are refunded before it re-reserves.
`coordinator` still doesn't opt into `reclaim_stale_running` at all — a
crashed slice has no reclaim path of its own, so it stays on the
sweeper's wall-clock backstop (below). `claude_docker`'s claim gained a
REQUIRED re-adopt branch: a reclaimed row that already carries
`meta.container` (a prior generation's launch survived independent of
the worker, conmon-style) is never relaunched — the next poll resumes
ownership by name-match; `ssh_node`'s detached submit/poll protocol
(below) has the analogous re-adopt guard on `meta.compute_handle`.

**Job containers carry CPU-limit flags (nice-all-jobs).** Every spawned
`docker/podman run` (`struct_relax`, `fold`, TTS, sandbox agents) splices
`container_limit_flags()` (`utils/container_limits.py`) right after `run` —
`--cpuset-cpus`/`--cpu-shares` from `PRECIS_JOB_CPUSET`/`PRECIS_JOB_CPU_SHARES`
(absent when unset, so a no-op by default). A container does *not* inherit the
host worker's `nice`, so this is what keeps a heavy fold/relax off the reserved
system cores — spark sets both to `2-19`, fencing cores 0-1 for sshd; the
systemd inference units (llama-swap/embedder/marker/ollama) get matching
`Nice`/`CPUAffinity`, the macOS plists `Nice`/`LowPriorityIO` (worker plists
stay `ProcessType=Interactive` for jetsam).

**The `sweeper` excludes the three lease-owning executors** (§H piece 6 —
was `ssh_node`-only, now `ssh_node` / `claude_inproc` / `claude_docker`,
`_LEASE_OWNING_EXECUTORS` in `sweeper.py`): the sweeper's wall-clock
`PRECIS_STUCK_JOB_HOURS` age arm fails an expired-lease `STATUS:running`
job outright, which would *race and win* the claim-side epoch/expiry
steal — stranding the compute result as `failed` instead of retrying it.
Now that all three self-heal via the boot-epoch mechanism above (a
same-node successor reclaims in one pass, a hang is still caught by the
expiry arm and capped by `poison_guard`), the wall-clock backstop is
redundant for them specifically. `coordinator` is deliberately NOT
excluded — it has no reclaim path of its own, so a crashed slice still
depends on this wall-clock sweep as its only crash recovery (documented
as a known gap in `executors/coordinator.py` — requeue-from-checkpoint is
future work). **Exception (gr172886 part-b): a distinct
`_reap_dead_node_orphans` pass** *does* terminalize an expired-lease
`ssh_node` job — but only when its `meta.params.target_node` host is
*provably dead* (no `precis-worker[-agent]` `worker_logs` within
`DEAD_WORKER_SILENCE_MIN` **and** no fresh `host_heartbeat`), the one case
where there is no live executor left to race at all. Same infra terminal
as the executor's own poison-guard (`STATUS:failed` +
`failure_class='infra'` + bubble → §C harvest re-dispatches), tagged
`reaped:dead-node-orphan`.

**`ssh_node` detached submit/poll protocol (§H piece 4,
`docs/proposals/compute-lane-lease-epoch.md`, gr187627, built).** A
job_type exposing BOTH `spec.submit`/`spec.poll` (`JobTypeSpec`) never
blocks a worker pass: `submit(ctx, spec) -> handle` launches the compute
DETACHED (the plugin owns HOW — `nohup`/`docker run -d`/`sbatch`) and
returns immediately; the handle is persisted to `meta.compute_handle`
and every subsequent `ssh_node` pass polls all this-host in-flight
handles (`poll(ctx, handle) -> bool`) — a cheap status check + lease
renewal, never a multi-hour block. A plugin with only the legacy
blocking `spec.dispatch` still works (backward compat — both precis-dft's
`gpaw_relax` and catpath's `autocatpath_seed` live out-of-tree and
haven't migrated yet), but `ssh_node` logs a deprecation warning (once
per job_type per process) naming gr187627 — the live incident where the
blocking call starved the claiming worker's whole pass rotation long
enough to trip host-dark.

**`wake_runner` child-deadlock deadline (§H piece 5, built).** A
`children_done` `Yield` gets `meta.wake_deadline` stamped at park time
(`executors/coordinator.py` — MAX of the children's own
`params.resources.wall_seconds` + margin, or `PRECIS_WAKE_DEADLINE_HOURS`
(default 6h) when none declare one). `wake_runner` re-queues a past-
deadline `waiting_children` parent "woken-degraded" — a
`child-failed:<id>` open tag on the PARENT job for every still non-
terminal child (visibility only, never a forced fail — the child's own
executor/sweeper still owns its terminalization), then `STATUS:queued`
so the coordinator's next slice resumes control. Closes the one
remaining "blocks forever" gap: a permanently-unschedulable child (no
live executor ever claims it) never reaches a terminal STATUS on its
own, so the plain `children_done` wake could never fire either.

**Two `precis worker` profiles.** (The "four LaunchDaemons" framing this
section used to carry — worker/watch/heartbeat/dream as four separate
daemons — is stale post-§A: `dream`'s standalone LaunchDaemon is retired,
folded onto the `scheduler` pass below; `heartbeat` is now BOTH a per-host
worker pass and its own still-live timer, pending §L.)

* `precis worker --profile=system` runs on every cluster node and
  drives every chunk-level + SQL ref-level pass: `embed`, `summarize`,
  `chunk_keywords`, `chase`, `fetch`, `gp_fetch`, `tag_embeddings`,
  `auto_check`, `schedule`, `scheduler`, `nursery`, `heartbeat`, `dispatch`,
  `sweeper`, `job_coordinator`, `job_ssh_node`, `wake_runner`, `clusterize`,
  `corpus_reconcile`, `paper_reconcile`.
  (`llm_summarize` is opt-in on top — env `PRECIS_SUMMARIZE_LLM=1` or
  `--only llm_summarize`; enabled on melchior as a deliberate trickle.
  `job_claude_docker` is opt-in on top too — env `PRECIS_SANDBOX_ENABLED=1`
  or `--only job_claude_docker`; default-OFF so the slice merges dark,
  meant only for the `agent_sandbox_host` nodes, **never melchior**. The
  agent-supplied `image` param is format-validated in
  `sandbox_run.semantic_rejection` (`_IMAGE_RE` — rejects a leading `-` /
  shell chars) and pinned behind a `--` sentinel in `build_run_argv`, so a
  `-`-leading value can't be parsed as a podman flag (gr179503).
  `inbound_chase` is opt-in on top too — env
  `PRECIS_INBOUND_CHASE_ENABLED=1`; default-OFF, dark until the global
  spend circuit breaker ships — see
  `docs/design/citation-chunk-grounding.md`. When on, it's the inbound
  counterpart to `chase`: the first read of a paper permanently marks it
  for a one-time, exhaustive sweep of its S2-known citers, locating +
  verifying the specific citing chunk in *both* directions
  — `chase`/`_chase_llm.py`'s `_locate_chunk_in_target`/
  `_verify_support_with_caveats` hooks, reused, not reinvented. Every
  `Handler`-direct kind (paper, draft, structure, cad, pcb, plan, pres,
  patent) also gained `view='links'` in the same slice
  (`handlers/_links_render.py`), closing the paper-link-blindness gap
  that pass depended on.)
* `precis worker --profile=agent` runs the passes that need the
  hermes OAuth / `~/.claude` state on melchior: the LLM-heavy
  reviewers (`structural`, `deep_review`) plus `job_claude_inproc`
  (planner-coroutine slice — moved off system 2026-06-15 so data-host
  workers stop claiming plan_tick/fix_gripe jobs they can't run and
  false-bubbling `child-failed`) and `quota_check`. It skips the
  embedder load it doesn't need. `quota_check` also **watches claude
  auth**: `claude_quota.refresh_snapshot` returns a `RefreshOutcome`,
  and a genuine 401 (`AUTH_FAILED`, distinguished from free-tier
  `NO_LIMITS` / transient `UNAVAILABLE`) raises a **critical**
  `quota_check:auth` alert (+ one-shot `notify_critical_alert`) so a
  stale/revoked OAuth token pages instead of silently 401-ing every
  agentic call for a day; auth recovering auto-resolves it.
* `dream_agent` (§A) no longer has its own standalone 15-min LaunchDaemon
  (`dream-pass.sh`, retired) — it's now the `dream_agent` **scheduler-lease
  cadence** (`workers/scheduler.py` CADENCES), host-pinned to melchior, whose
  interval IS the live `dream.min_interval_minutes` knob (`resolve_interval`
  reads `workers/dream_throttle.resolve_min_interval_minutes` directly:
  `app_settings` DB > env > compiled 15; web-set on the Budget sub-tab,
  `POST /budget/dream-interval/set` — Wave-0 §G of
  `docs/proposals/cluster-scheduling.md`). A cheap `eligible()` gate
  (`PRECIS_DREAM_AGENT` truthy AND the soul file readable) keeps melchior's
  *system*-profile worker from ever winning the lease — only the
  *agent*-profile process (which carries the env + OAuth) can claim it; the
  worker-agent role now installs `PRECIS_DREAM_AGENT` / `PRECIS_DREAM_SOUL_PATH`
  on that plist. §G's in-pass `skip_if_too_soon` throttle stays as
  belt-and-suspenders (and still guards a manual `--only dream_agent` run).
  Each tick still injects a per-cycle **quest-anchor** nudge — a random
  active quest seeds one of the two anchors, `angle≈0.5`, other leg stays
  free; `PRECIS_DREAM_QUEST_ANCHOR`/`_ANGLE` — and opens a `kind='agentlog'`
  provenance node whose id rides `env_overlay` so its spawned websearch /
  memory refs attribute back via `touched`.
* `anki_sync` (§A) folds the same way — a `scheduler` cadence host-pinned to
  melchior, `eligible()` gated on `PRECIS_ANKI_ENABLED` + the `anki` wheel
  being importable, `run` delegating to `workers/anki_sync.py::run_anki_sync`
  (the same guts `precis anki-sync` uses for a manual run — the CLI now
  delegates to it too, no `sys.exit` in the shared core). The standalone
  30-min `com.precis.anki-sync` LaunchDaemon is retired; the pg advisory lock
  still serializes a cadence-fired tick against a concurrent manual run.
* `cron_tick`/`watch_poll`/`health_digest` are the other unpinned `scheduler`
  cadences (§15i — any live worker can win them): `cron_tick` fires due
  recurring (`meta.schedule` set) ticks (queue-mode spawn or `meta.deliver`
  push) via `run_schedule_pass`, not the retired `kind='cron'` engine;
  `watch_poll` polls S2 for citing papers; `health_digest` (§D, hourly)
  fires one `health_digest` liveness-net eval — see the `Review tiers`
  section above. The `scheduler` pass itself is now default-on
  for BOTH profiles (`registry.py`) — the agent profile must run it too, or
  the melchior-pinned cadences above never have an eligible claimant.
  Each heavy pass dedups on its tier-tagged memory and load-gates on
  `PRECIS_LOAD_CEILING` (default `os.cpu_count() * 1.5`).
* `heartbeat` (§A) is now ALSO a per-host `system`-profile worker pass
  (`workers/heartbeat.py::run_heartbeat_pass`), not just the standalone
  launchd (Macs) / systemd-timer (spark) reporter — deliberately NOT on
  `scheduler_leases` (it's the liveness signal that lease/claim machinery is
  judged by), self-throttled via an in-process timestamp
  (`PRECIS_HEARTBEAT_INTERVAL_SECONDS`, default 60s). The still-live timers
  keep firing too (retiring them is §L) — a double-fire is a harmless
  idempotent UPSERT.

**Notable passes:**

* `cast_audio` — the daily audio **casts** (docs/design/reading-prep-loop.md
  §Audio). Two standing casts ride one produce→narrate→publish spine, two voice
  profiles: **`reading`** (morning situational-awareness brief, `bm_george`,
  ~20 min — `reading/briefing_cast.py` unions news/activity/reading/recall/quest
  lanes, each degrade-to-empty; depth-first prompt, papers carry abstracts + a
  `[[pa<id>]]` cite marker (dropped inline per claim → a `§` link in `/drafts`,
  stripped from audio by `narrate.speakable`) + true overnight paper count (not
  the naming cap), the reading lane names papers opened in the web reader
  recently (`chunks.last_seen` past its ingest-time default — the "where you
  left off" nudge, distinct from the overnight-acquired papers above), leech
  cards carry bodies, active-only quest report with a decaying dormant nudge
  that links its strivings; papers/findings
  `cites`, news wire `derived-from`, drafts/quests `related-to`) and **`nidra`**
  (evening concept-graph meditation, `af_nicole`, ~45 min segmented walk —
  `reading/meditation.py`; walked concepts `related-to`). Producers
  persist a standalone dated `draft` marked `meta.cast` and **link it back to the
  sources it drew on** via the shared `cast_common.link_sources` (a cast reads no
  URL aloud, so these graph edges — plus the morning brief's inline `[[pa…]]`
  paper citations — are the durable pointer back; `links_for` the cast draft
  reopens them; best-effort, a bad edge is skipped); `workers/cast_audio.py`
  (spark, default-OFF `PRECIS_CAST_AUDIO_ENABLED` + `PRECIS_TTS_IMAGE`) narrates
  any un-narrated cast draft via `render_narration` → `render_episode` →
  `publish_episode(source=profile.source)` — a **distinct** producer tag per cast
  (`brief` / `meditation`, so a shared feed can subfilter), idempotent on `meta.audio_episode_id`
  (sibling to `briefing_audio`). The episode id + on-demand PDF filename are the
  human `export_stem` (`morning_brief_<date>` / `evening_meditation_<date>`), not the
  internal `cast-*` slug (`cast_common.export_stem`). On creation `create_cast_draft`
  files the draft under a per-cast Drive **folder** ("Morning brief" / "Evening
  meditation", find-or-create, best-effort) so its text shows in `/drive`; the Drive
  row surfaces the published mp3 + compiled PDF as download links. Compose is the
  `reading_brief`/`meditation`
  **`claude_inproc`** job_types (melchior — both casts, `card_forge`, and the news
  briefing now compose via the LLM router's `Tier.FRONTIER` (`DispatchClient`,
  ADR 0046) onto `claude_agent` — a `claude -p` subprocess, direct Anthropic OAuth —
  not the melchior-loopback litellm proxy; litellm now serves only `SMALL`. The two
  audio **casts** default to `claude-sonnet-5` within the FRONTIER band — prose
  composition, ~⅕ the subscription-quota draw; `card_forge`/news briefing keep the
  FRONTIER Opus default. That cast default now lives in the per-operation registry
  (`utils/llm/operations.py`, runtime-tunable via `llm.op.reading_brief`/`meditation`),
  not a call-site `model=` arg — see the per-operation routing note in the LLM-router
  section)
  on daily recurring (`meta.schedule` set) watches; **TTS is the separate downstream spark pass**, so the
  nice-model compose and the container narration never block each other. CLI:
  `precis cast run <reading|nidra> [--publish]` + `precis cast schedule [--now]`.
  Skill: `precis-audio-help`. A third daily watch rides the same installer:
  **`card_forge`** (05:30, before the brief) — the morning card work
  (`reading/cards.py`): mastery-from-Anki refresh (`reading/mastery.py`:
  `represents`-linked cards' `anki_stats` → concept `meta.mastery`/`state`),
  the retire / teach-prereq / escalate / rewrite ladder over ≥4-day leech cards
  (streak + escalation **auto-reset** once the concept's cards prove healthy —
  no leech and ≥1 reviewed card past the proving window)
  (**observe-first** — `PRECIS_CARD_FORGE_AUTONOMY=report` default, `act` to
  apply; a retired ref's Anki note is removed own-guid-only by the sync tick),
  then minting `PRECIS_READING_CARDS_PER_DAY` (5) concepts' worth of new cloze
  cards (`represents`-linked, riding `precis anki-sync`). The mint applies a
  **cardability gate** (the `precis-cloze` rule-0 taxonomy): a concept that is a
  topic-label / stock phrase / front-matter / one-of-many example is refused —
  the model returns empty cards + a `skip_reason`, which stamps
  `concept.meta.card_forge_skip` so `_cardless_concepts` never re-offers it (no
  daily call re-spent refusing the same junk). The brief's recall
  lane reports forged cards + escalated concepts; the nidra walk orders by
  mastery (`prefer_mastered=True`) — the evening drift through what you know.
* `llm_summarize` — model-authored two-part summary (gist + a
  sentence of detail) into `chunk_summaries` under
  `summarizer='llm-v1'`, distinct from the lexical `rake-lemma` row
  and the per-chunk KeyBERT keywords. A ref-pass (own claim/writes),
  not a pure `WorkerHandler`. Registered by
  `0025_register_llm_summarizer.sql`.
* `sweeper` — fails `kind='job'` rows whose `STATUS:running` is older
  than `PRECIS_STUCK_JOB_HOURS` (1.0h), tagging `swept:claim-orphaned`
  so the parent's failure-bubble unblocks the cascade. Recovers
  deploy-time claim orphans — **except the three lease-owning executors**
  (`ssh_node` / `claude_inproc` / `claude_docker`, §H piece 6), which each
  reclaim + retry their own crashed jobs via the boot-epoch mechanism
  (see the crash-recovery note above); `coordinator` still relies on this
  wall-clock sweep.
* `corpus_reconcile` — maintains the per-host `pdf_locations` presence
  ledger (migration 0052). Each node stats the held-paper PDFs under its
  own `PRECIS_CORPUS_DIR` roots (preferring `pdfs.storage_path`, falling
  back to the `corpus_pdf_dest` cite_key convention) and records a verdict
  per `(pdf_sha256, host)` — the path found, or `''` for checked-and-absent.
  The draft reader's held-but-missing ▲ then reads that ledger
  (`Store.pdf_missing`: checked-yet-no-fresh-copy) instead of re-stat-ing at
  request time, so the marker is a corpus-wide fact independent of the web
  host's mounts (ADR 0029). Self-throttling via a refresh window
  (`PRECIS_CORPUS_RECONCILE_REFRESH_HOURS`, default 6, ≪ the ledger TTL
  `PRECIS_PDF_LOCATION_TTL_DAYS`, default 7); idle once every verdict is
  fresh. No-op on a node with no corpus roots.
* `paper_reconcile` — the standing dedup sweep behind `precis
  reconcile-duplicates`, now on a cadence (it was manual-only). Folds
  duplicate paper refs into the survivor across three classes: shared
  `pdf_sha256`, DOI-modulo-case, and **id-less title-only stubs that
  duplicate a held paper** (`dedup.reconcile_by_title_similarity`, the
  Phase-3 near-dup case — auto-merge only the high-confidence band, the
  rest surfaced for review). Prevention is upstream in
  `Store.upsert_stub_paper` (a title-only acquire fuzzy-matches held
  papers first). Cheap between runs: an `app_state`
  `paper_reconcile:last_run` marker gates the pass to once per
  `PRECIS_PAPER_RECONCILE_REFRESH_HOURS` (default 24), and a single-runner
  `pg_try_advisory_lock` keeps just one node sweeping corpus-wide. The same
  pass also runs the deterministic **hygiene heals** (`ingest/paper_hygiene.py`):
  rebuild drifted `card_combined` chunks (title repaired but the embedded
  search card never rewritten), collapse `superseded_by` chains onto the
  final live survivor, repoint non-`supersedes` links off soft-deleted
  papers, and **re-queue stranded OA fetches** (`requeue_stranded_fetches`
  — a stub that logged `fetch_ok` but never ingested, i.e. `pdf_sha256`
  still NULL, older than `PRECIS_OA_STRANDED_HOURS` (default 48): the
  pre-2026-06-19 inbox-misconfig signature. Deletes the stub's `fetcher:%`
  events to reset the exponential backoff so the fixed pipeline re-fetches,
  stamping a one-shot `meta.oa_requeued` guard so a re-failure can't spin).
  See `docs/design/duplicate-paper-handling.md` (Phase 3). §A retired the
  redundant nightly `com.precis.reconcile` LaunchDaemon on caspar
  (`precis reconcile-duplicates --apply`) as a pure retirement — no new
  cadence — since this pass already covers the identical dedup sweep with
  its own throttle + advisory lock; migrating ITS `app_state` throttle onto
  the scheduler lease is §E, not done here.
* `fetch` / `chase` backoff — **both exponential**. The OA fetcher's
  retry window arms on any `fetcher:%` event (not just `unpaywall`,
  which is disabled in prod) and doubles per prior attempt
  (`base * 2^(attempts-1)`, capped). Finding-chase skips a `waiting`
  finding inside an equally-exponential window — `WAITING_BACKOFF_MINUTES`
  (60) doubling per consecutive `waiting` up to `WAITING_BACKOFF_MAX_MINUTES`
  (1440), the run resetting on any non-`waiting` outcome. Both fixes
  kill `ref_events` spin-loop floods. NB the fix only helps once
  *deployed* — prod ran pre-fix code well after the merge, so a
  spin-loop digest spike usually means "redeploy", not "new bug"
  (check the deployed sha under `~deploy/.cache/uv/git-v0/checkouts/`).

**Unified `claude -p` agentic dispatch — `utils/claude_agent.py`.**
Peer to `utils/claude_p.py` (one-shot JSON judge). Carries the
agentic flag set (`--mcp-config` / `--strict-mcp-config`,
`--append-system-prompt`, `--max-turns`, `--permission-mode`,
optional `--bare`, `--disallowed-tools`) + cost cap + wall-clock
timeout + structured `log_event` to `ref_events`. The (untrusted —
asa passes raw Discord text) prompt is the sole trailing positional,
emitted after a `--` end-of-options sentinel so a dash-leading message
can't be parsed as a CLI flag (same guard in `claude_p` / `tex_llm_fix`). The reviewers,
`dream_agent`, and the web "ask a follow-up" path all share this
surface. Stub-binary tests via `PRECIS_CLAUDE_BIN`. A non-zero exit
that is a **resumable exhaustion** — the `--max-turns` ceiling or the
`--max-budget-usd` cap, detected via the trailing `stream-json` result
event (`_recoverable_exhaustion`) — is **recovered, not raised**: the
wrapper returns the partial `AgentResult` (final text via the result
event, falling back to the last assistant message rather than dumping
the raw JSON stream), mirroring how `plan_tick` treats exhaustion as
resumable. This stopped the follow-up "ask & think" path surfacing a
bare `⚠️ thinking failed: …exited 1:` whenever the agent ran out of
turns. Genuine errors still raise — now with the `terminal_reason`
folded into the message, since stream-json errors leave stderr empty.
**Container executor gate (§13/§15d).** When `PRECIS_AGENT_CONTAINER` is set
the SAME `claude -p` runs in a throwaway `precis-agent` container
(`workers/executors/agent_container.py`) — every host-built flag passes
through verbatim EXCEPT `--mcp-config`, which `_rebase_mcp_config()` rewrites
onto the image's baked-in `default_agent_mcp_config()` (`/etc/precis/agent-mcp.json`,
`docker/Dockerfile`); the host's `PRECIS_MCP_CONFIG` path (e.g.
`/Users/deploy/.claude/mcp.json`) has no meaning inside the container
filesystem (2026-07-24 incident: a daemon restart reset the health latch below,
first live activation of this path forwarded the host path unrebased and every
containerized pass 404'd on "MCP config file not found"). The opt-in is now
gated behind a
**verified-capability probe** (`container_capability_ok()`: auth token
resolvable ∧ `<bin> info` ∧ `<bin> image inspect` — per-process ~60s-cached,
fail-safe to in-proc), so an opted-in host that can't actually containerize
runs in-process instead of failing every pass. A containerized run's
**infra** failure (image-missing/daemon-unreachable/socket-perm/**OOM 137**,
vs. a claude/model error) trips a ~10-min health latch (`trip_container_unhealthy`)
and retries the same call in-process once — catching the OOM 137 here keeps it
off the router's `interrupted` (`rc>=128`) skip path. Flag stays opt-in
(unset=OFF); auto-detect retirement is the remaining follow-on. On an opted-in
host the heartbeat also advertises a soft **`container_agent`** gauge
(`capability_probe.probe_soft_signals`, capacity 1) — `1` = verified, `0` =
degraded (opted in but can't launch) — which `/factory` renders as a green/red
"agent" chip so a silently-in-proc host reads as degraded, not silent.
`call_claude_agent` also takes an optional `env_base` (isolated env instead
of `os.environ`), `mounts`/`workdir` (`agent_container.Mount`, threaded to
the container path only), and `require_container` (§H cycle a — refuse
with `ContainerRequiredError` instead of silently falling back in-process,
for a caller that can't tolerate running unsandboxed; see fix_gripe below).

**LLM independence — the switchable router (`utils/llm/`, ADR 0046).**
Every routed call goes through `dispatch(LlmRequest)` → a narrow
`LlmProvider` port (`run(req, *, model) -> LlmResult`) picked from a
`Transport`-keyed registry. `claude -p` is now just two adapters
(`ClaudeAgentProvider`/`ClaudePProvider`) among peers — Anthropic is a
swappable leaf. A `Backend` switch (`PRECIS_LLM_BACKEND`, default
`anthropic`, **ships dark**) flips cloud work to an **OpenAI-compatible
OSS backend** (OpenRouter/DeepInfra/remote vLLM at `PRECIS_LLM_BASE_URL`,
API key from the secrets vault via `get_secret('PRECIS_LLM_API_KEY')`):
tool-less calls → `OpenAICompatProvider`, tool-using calls →
`OpenAIToolsProvider`. The latter is the OSS **`tools=` agent loop**
(`utils/llm/openai_tools.py` engine + `precis_tools.py` bridge): it
advertises the precis verbs from `TOOL_REGISTRY` as OpenAI function
schemas and executes each tool call **in-process** via `runtime.dispatch`
(no MCP socket round-trip), rebuilding ADR 0024's reversed loop behind the
port. Model ids resolve from the same `PRECIS_MODEL_*` table. **Both the
backend and the per-tier model are live-switchable** (`utils/llm/live_config`):
`resolve_backend`/`resolve_model` layer an `app_settings` DB override
(`llm.backend` / `llm.model.<tier>`, keys the `/factory` console writes) over
the env default — TTL-cached ~15s, read from the breaker's bound store
(`meter.active_store()`), so a flip reaches the whole fleet within one TTL, no
redeploy. Dark: no row (or no store) → env, byte-identical. With the backend
unset, behavior is byte-identical to `claude -p`. **Unit 4b (call sites folded through the seam) is done**:
dream, the structural/deep reviewers, cad_propose/cad_discuss/
structure_propose, the web follow-up (`precis_web/ask`), and the
`claude_p` judges (chase, good_search triage, figure) all call
`dispatch(LlmRequest)` now — so `PRECIS_LLM_BACKEND` switches the whole
agentic + judge surface. **`plan_tick`** keeps its own spawn seam (neutral
cwd + env back-doors + `acceptEdits`, no friction footer — ADR 0051 §12)
but now **forks on `resolve_backend()`**: default `anthropic` = the
byte-identical `claude -p` spawn; `openai` (+ base url) runs the tick over
the in-process `OPENAI_TOOLS` loop (`run_oss_tool_loop`), binding its
runtime context (parent todo / workspace / model / agentlog) through a
**thread-isolated `ContextVar`** (`utils/inproc_context.py`) instead of the
subprocess env the in-process loop can't carry — the env-readers
(`workspace.current_*_from_env`, `agentlog.current_from_env`) consult it
first, env otherwise, so the spawned-claude + operator/test paths stay
byte-identical. `max_turns` maps to a resumable `PlanTickOutcome`
(`resume_reason`) so the executor streak-cap still fires. (Known gap: the
OSS tick skips the prose-kind gate — boot-time only — so the `## Draft`
prompt block is its sole steer there.) **Fleet-flip safety**
(`docs/proposals/glm-fleet-flip-safety.md`, landed 2026-07-25) closes three
`backend=openai` coherence gaps found on a live flip: **Part 1** — `dispatch`
transparently remaps `SMALL`'s local-only aliases (`summarizer`/`rake-lemma`)
to a configured hosted small model (`llm.model.small` override →
`PRECIS_LOCAL_SMALL_HOSTED_MODEL` → default `z-ai/glm-4.7-flash`) whenever the
call lands on a hosted OSS transport (`router.py::_hosted_small_remap`) — a
no-op under default `anthropic` or a local `served_by` slot. **Part 2** — the
`openai_tools` loop now captures OpenRouter's `usage.cost` (falling back to
the token price table) into `LlmResult.cost_usd`/`llm_call_log.cost_usd`
(`openai_tools.py`, `router.py::_dispatch_openai_tools`), so the budget
breaker isn't blind to OpenRouter spend. **Part 3** — `resolve_model(tier,
backend=)` (`router.py::resolve_model`) is backend-aware: under an effective
`ANTHROPIC` backend it drops an incoherent OSS `app_settings` model override
for `FRONTIER`/`BIG`/`MEDIUM` only (`SMALL`'s override is always honored — it
never routes to a claude transport), so a half-applied flip (backend demoted
for a missing `PRECIS_LLM_BASE_URL`, model override still OSS) never hands an
OSS slug to a claude transport. The two sites whose `--model` assumes claude
semantics regardless of the chokepoint — `fix_gripe` (routed through
`call_claude_agent`, §H cycle a, but still pins a Claude-only model) and
`sandbox_run`/`claude_docker` — read `resolve_backend()` and skip clean (no
spawn, job marked skipped/cancelled, not failed) under `backend=openai`
rather than being folded through `dispatch`. **Built: the
`FailoverProvider`/`Rung` ladder**
(`PRECIS_LLM_FAILOVER`, off by default) wraps an OSS primary transport with
an automatic claude-fallback rung on a transport error, pinned to that tier's
own compiled claude id (`router.py::_claude_default` reads
`_TIER_MODEL[tier][1]` directly, ignoring any `PRECIS_MODEL_*`/live override);
`SMALL` gets no claude rung at all. The ladder also covers a **saturated
local slot**: `dispatch()`'s paused-slot branch (`local_serving.acquire()`
returns `paused=True`, all capacity busy) retries the ladder's rung 0 with the
request's `local_url` override cleared, landing on the hosted OSS endpoint
instead of the busy local hardware, before falling to claude if that also
errors (`docs/proposals/llm-openrouter-bypass.md` item 3). `select_transport`
routes `SMALL` to `OPENAI_COMPAT` under `backend=openai` (item 2), vs. the
loopback litellm proxy it takes under the default `ANTHROPIC`. **SMALL judge
pins reasoning off:** a tool-less `SMALL` call with no explicit `effort`
merges `reasoning:{enabled:false}` into the `openai_compat` body
(`router.py::_dispatch_openai_compat`) — a reasoning model on that rung
(`z-ai/glm-4.7-flash`, tier-floor medium) otherwise spends the whole
`max_tokens` budget on a reasoning trace and returns empty `content`;
`LlmClient.complete` now normalizes null/omitted content to `""` (not the old
`str(None)`=`"None"`, a 4-char pseudo-answer that slipped past every
empty-check and silently failed while `errored` stayed false), so the caller's
empty-handling fires (classify no-value, summarize's `EmptySummaryError`
retry). **SMALL local calls also fail fast:** `_dispatch_local` caps a
`SMALL` call's loopback timeout at `_SMALL_LOCAL_TIMEOUT_S` (30s, vs the 120s
`LlmConfig` default; an explicit `req.timeout_s` still wins) so a
stuck/flapping `:4000` proxy fails fast → the `FailoverProvider` falls over to
the hosted rung, instead of a batch (N chunks × 2 calls × 120s) blocking past
the worker watchdog and stranding the pass (the 2026-07-26 classify stall).

**ADR 0066 — operator placement-chains, all phases shipped.**
`live_config.chain_override(tier)` + `router.py::resolve_chain` layer a
per-tier `app_settings`-backed chain override in front of the compiled
default; `dispatch`/`dispatch_async` resolve *every* call through it, so an
operator `llm.chain.<tier>` override is read **regardless of
`PRECIS_LLM_FAILOVER`**. With no override, `resolve_chain` → `_default_chain`:
a single primary rung by default (byte-for-byte the non-failover path), or
`_failover_ladder` when the flag is on. `dispatch` wraps in `FailoverProvider`
iff `_failover_enabled() or len(chain) > 1 or chain[0].model is not None` —
so an operator single-rung chain (every parsed override rung pins a model) is
honoured too. **Cloud throttle (§5):** `live_config.cloud_enabled()`
(app_settings `llm.cloud_enabled`, default true) +
`router.py::_apply_cloud_throttle` prune a resolved chain's cloud rungs when
an operator disables cloud — `_rung_is_cloud` classifies by explicit operator
`placement` label, else by transport (claude = cloud; litellm = local; OSS =
cloud iff `PRECIS_LLM_BASE_URL` set). A tier with a local rung keeps flowing
on it; a tier left with no rung prunes to empty → `dispatch` returns `paused`
(skip-not-fail, never silently degraded). **Which tiers survive depends on
their chain:** `FRONTIER` is always cloud-only (pauses); today only `SMALL`
has a standing local rung (`LITELLM`), so `BIG`/`MEDIUM` also pause under
throttle until an operator chain gives them a `placement:"local"` rung (the
target-state "drop to local" story lands with the Phase-3 roster / chain
editor). No-op while cloud is on (byte-identical). `/status?tab=services`
carries the operator placement-chain editor (a per-tier JSON textarea `POST
/factory/llm/chain` writing `llm.chain.<tier>`, server-validated list-only,
blank = revert) plus a cloud-throttle toggle (`POST /factory/llm/cloud`
writing `llm.cloud_enabled`) — `status.py::_llm_chain_ctx`, degrade-safe.
**Phase C (landed) retired the five location-coupled tier members** —
`router.py::Tier` now has *only* `FRONTIER`/`BIG`/`MEDIUM`/`SMALL`; every call
site routes on capability, never location (a served OSS model backs `BIG`
when the backend/chain routes there, not a separate `LOCAL_BIG` tier). A
*stored* pre-Phase-C tier string (a quest's `meta.loop.tier`, a job's
`meta.params.tier`, a route-log row) degrades onto its capability analogue via
`router.tier_from_str()` + `_LEGACY_TIER_ALIASES` instead of raising —
`local-small`→`SMALL`, `local-big`/`cloud-mid`→`BIG`, `cloud-small`→`MEDIUM`,
`cloud-super`→`FRONTIER`. Phase C also folded in the two pieces Phase B
deferred: `llm_catalog.seed_default_cards` now seeds one `tier_floor` card per
capability tier (`for tier in (FRONTIER, BIG, MEDIUM, SMALL)`, no more
first-wins clobber over five tiers), and the four capability `meta.llm_tier`
aliases (`frontier`/`big`/`medium`/`small`) are live in `PLANNER_TIER_BY_ALIAS`
alongside the legacy `opus`/`sonnet`/`haiku`/`local` names (`local` now pins
`BIG` directly). The legacy GLM-preset panel (`_llm_override_ctx`) is gone.
**Failure semantics (§5a):** a transport exception is classified
(`router.py::_is_unavailability`) — timeout / connection / HTTP 5xx / 429 →
`paused` (skip-not-fail, the todo retries next cycle, never parks); HTTP 4xx
(semantic) stays `error`. Applies to the OSS transports and, via
`ClaudeProcessError.timed_out`, to a `claude` wall-clock timeout too — so a
claude-only rung (`FRONTIER`) waits rather than parking. Every transport
already carries a wall-clock timeout (claude 600 s, openai_tools / litellm
120 s), so a hang converts to that classified failure.

**Per-operation routing (Phases 1+2 landed, `docs/proposals/llm-operation-routing.md`).**
The rung *between* the tier default and a call-site `req.model` pin, keyed on
`req.source`. `utils/llm/operations.py` is an **opt-in allow-list** (`LLM_OPERATIONS`)
of `dispatch()`-routed, pin-free operations + their code defaults (`OpDefault(tier,
model, env, label, …)`); `live_config.op_override(source)` reads a live
`app_settings` `llm.op.<source>` JSON (`{tier?, model?}`). `dispatch` calls
`operations.resolve_op(req.source)` just before the tier resolve: a *registered*
source has its effective tier remapped + model resolved DB-override > legacy `env`
hatch > registry literal (a *fallback* into `model or resolve_model`, so a
`llm.chain.<tier>` rung's own model still wins); a *non-registered* source (incl.
functional pins like `classify`→`"summarizer"`, and router-bypassers like
`fix_gripe`, both in `EXCLUDED_OPERATIONS`) returns `None` → today's `req.model or
resolve_model` path untouched, so it **ships dark**. The two casts' Sonnet-5 default
migrated off their `model=` arg into the registry (`reading_brief`/`meditation`).
CLI: `precis llm op {list,set,clear}`. **Phase 2 (landed):** the
`/status?tab=services` ops panel (`status.py::_llm_ops_ctx`/`_llm_op_stats`,
`factory.py::set_llm_op` = `POST /factory/llm/op`, `_status_services.html.j2`) —
one row per op over union(registry, observed `llm_call_log.source`) sorted last-run
desc, steerable ops editable (`default/frontier/big/medium/small/pinned` + model
picker, blank/`default` clears), excluded/unregistered read-only with reason,
`effective` steerable-gated. Still open: AC6 full `source=` drift scan; routing
`fix_gripe` through `dispatch()`.

## Discovery layer (F20)

Per-chunk KeyBERT supersedes the dropped `ref_segments` /
`ref_segment_sentences` tables (migration `0003_drop_legacy_segments`;
ADR 0018 status note):

- `chunks.keywords TEXT[]` (canonical lower-case forms, GIN-indexed)
  + `chunks.keywords_meta JSONB` (versioned envelope: short/long pairs
  + KeyBERT scores). Worker: `chunk_keywords` (claim shape
  `keywords IS NULL OR keywords_meta->>'version' != current`, so
  bumping `KEYWORDS_VERSION` lazily re-claims the whole corpus).
- `view='toc'` (papers): DP-clusters the keyword arrays at request
  time — `src/precis/utils/toc_db.py` `render_from_store`. No
  precomputed segment rows.
- `view='toc'` (skills): per-request DP+KeyBERT via
  `src/precis/utils/toc.py`, memoised per `(slug, scope)` since skill
  files are static for the process lifetime.
- Search no longer reranks against `ref_segment_sentences`; result
  rows carry no `excerpt @ ~N` sub-lines.

Policy: `docs/conventions/discovery-layer-policy.md` (F20-rewritten).

## Source-backfill (gap-finder)

Design: `docs/design/source-backfill.md`. Package `src/precis/backfill/`
— the **recall** mirror of the citation verifier ("did I miss anything?"
vs "is what I cited true?").

- **Recall lenses + workspace** — built. `text` lens (semantic+lexical
  sweep over the target chunk(s), `candidates.py::find_candidates`) +
  `citation` lens (S2 citation-graph one-hop neighbours of what's cited,
  materialized into `links`); Tier-0 dedup against the draft-wide cited
  **and** dismissed sets (`draft_cited_ref_ids` / `dismissed_ref_ids`);
  assembled into the ADR-0051 eyes/working-set composer with `★
  cited`/`○ candidate` roles + a ✓/⚠ grounding block
  (`workspace.py::assemble` / `render_backfill`). Section-scoped:
  `get(kind='draft', id='dc<id>', view='backfill')`.
- **`[fi]` claim-hub-aware closure (Build 2 §G1)** — built.
  `draft_cited_ref_ids` folds in every cited `[fi<id>]` hub's
  evidence-supporter papers (`_hub_supporter_ref_ids`) — otherwise a
  paper already backing a hub the draft cites would re-surface as a
  false "gap" once Taproot backfill converts `[pc]`/`[pa]` cites to
  `[fi]` (see Taproot canonicalization, below).
- **Whole-draft roll-up (Build 2 §G2)** — built. `get(kind='draft',
  id='<slug>', view='backfill')` (no longer section-only) runs the
  section-scoped sweep once per top-level section (`assemble_draft`)
  and merges by source ref (`merge_recurrence`) — a source recalled
  across multiple sections ranks first, the closure is draft-wide.
  Deliberately a slimmer aggregate render than the section view
  (`render_backfill_draft`) — full eyes/grounding detail stays
  per-section (`view='backfill'` on a `dc<id>`).
- **Topic/categorizer precision gate (Build 2 §G3)** — built.
  `draft_topic_slugs` derives the draft's dominant `topic:<slug>`
  tag(s) from its cited-paper closure; `_apply_topic_gate` confirms
  on-domain hits (`LENS_TOPIC` folded into `.lenses`) and demotes
  (never drops) off-domain/untagged ones below them — keeps a project
  like "nanobuds" from drifting into adjacent-but-different corpus
  neighbours (nanoribbons, general graphene). **Degrades to a no-op**
  (existing semantic+citation ranking) when no cited paper carries a
  `topic:` tag — `classify_topics` is a dark/default-OFF pass (above),
  so topic coverage may be sparse-to-absent corpus-wide.
- **Not yet built:** model-authored recall lenses (HyDE, the Tier-1
  relevance cull) and the **integrate** coroutine that weaves accepted
  candidates into the draft — FIND + WORKSPACE only today, no
  auto-weave.

## Chunk-tag classifier (ADR 0047 cascade)

Controlled chunk/paper tags written by a measured **cascade**, not a
single model. Axis defs live in `src/precis/data/axes/*.yaml` (id +
values + prompt + few-shot + `applies_when`); gold sets + accuracy live in
`scripts/classify/` (`gold_set/`, `eval-classifier`, `EVAL_RESULTS.md`).

- **Why a cascade.** The free local model (`summarizer` alias) is ~72% on
  the 11-way `role` — it fails the *attribution test* (own-work vs
  others') — but 94% at junk (furniture vs substance) and **88% /
  91%-own-precision** at the 3-way collapse **`role3`** (own / background
  / furniture). Human agreement is ~89%, so ~85-90% is the ceiling; the
  residual is real ambiguity, absorbed by gold `accept:` sets + the
  query-time agent. So the cheap model does the coarse, high-value calls
  and a stronger model is reserved for the narrow residual.
- **Tiers.** 0: free regex drops furniture (~24% of prod). 1: `junk` gate
  → `role3`, local, cheap. 2 (optional, gated): re-judge `own` chunks with
  a stronger model (`--escalate-model` / `PRECIS_CLASSIFY_ESCALATE_MODEL`).
- **Writes** `Tag.closed("ROLE3", own|background|furniture)` → `chunk_tags`
  (`pos=ord`, single-valued). `ROLE3:own` is the citation-grounding filter
  (91% precision) — use as candidate-gen/soft-boost, verify with the agent,
  never a lone hard precision gate.
- **Pass.** `workers/classify.py` `run_classify_pass` (self-contained
  ref-pass like `llm_summarize`; `chunk_claims` artifact
  `classify:cascade-v<CLASSIFY_VERSION>`, idempotent, reversible),
  registered in `cli/worker.py` **default-OFF** (`PRECIS_CLASSIFY_ENABLED=1`
  / `--only classify`). Global FIFO sweep by default; `run_classify_pass`
  takes optional `ref_ids=` to scope to named papers, driven by `precis
  classify role3 --cites-of <draft> | --topic <slug> | --ref-ids <csv>`
  (runs to completion over the resolved set — targeted single-dossier
  backfill, mirrors `classify_topics`' `ref_ids` scoping). Manual backfill +
  eval: `scripts/classify/classify --cascade` (dry-run default; `--commit` to
  write). Full design: `docs/design/chunk-classifier-cascade.md`.
- **Generic axis runner (ADR 0047 §3, built).** `workers/axis_pass.py`
  (`run_axis_pass`) drives every `data/axes/*.yaml` axis outside the
  `junk`/`role3` cascade — chunk-level (claim/lease mirrors
  `classify.py`) or ref-level (claim mirrors `classify_topics.py`,
  the default when an axis omits `level:`). Enforces both `prereq:`
  (the item must already carry a tag in each listed prerequisite
  axis's namespace, checked at *that* axis's own level) and
  `applies_when:` (`domain_in` value filter, `tags_any` gate) —
  neither existing pass enforced these. `tags_any` resolves ref-level
  by default, but for a **chunk-level axis** it checks `v_chunk_tags_all`
  on the chunk (2026-07-25), so a chunk axis can gate on another chunk
  axis's *value* — e.g. `open-question` now runs only on
  `ROLE3 ∈ {own, background}` chunks, skipping furniture. Same pass, the
  axis *definitions* were also reviewed/tightened that day: `material`
  widened (metal/zeolite/2d-material — catalysts no longer fall to
  `other`), `transport` gained `unknown` + a `PROPERTY:electrical|multi`
  gate. `discover_axis_ids()` is the
  one source both `cli/worker.py`'s per-axis wiring and the
  `/categorizers` console read; each id registers its own `axis:<id>`
  `service_config` service, default-OFF (`PRECIS_AXES_ENABLED`
  comma-list seeds the default-on verdict). The ~10 axes this makes
  runnable (`domain`, `scale`, `dim`, `transport`, `material`,
  `property`, `studytype`, `move`, `open-question`, plus any new
  axis file) still haven't been swept corpus-wide — flipping one on
  is an operator decision, not yet exercised in prod.

### Topic-dossier classifier (ADR 0060 cascade) — classifier slice built

Same cascade shape lifted one level, **paper**→topic (not chunk→role), for
the standing "living topic document" line of work. Taxonomy config
`src/precis/data/topics/*.yaml` (one file per top-level topic — 14 as of
2026-07-25: `healthspan`, `molelec`, `noxrr`, `nh3-synthesis`,
`co2-conversion`, `catalyst-stability`, `mof`, `mof-tools`,
`catalysis-tools`, `nanobuds`, `carbon-cad`, `llm`, `ml-general`,
`bayesian-statistics` — each with `slug`/`description`/`keywords`/`sub_tags`;
the top-level list is **closed**, new entries added by Reto, not auto-minted,
per ADR 0047's measured folksonomy-drift lesson). The set was reviewed +
tightened 2026-07-25 (`CLASSIFY_TOPICS_VERSION=3`): `llm-improvements`
rescoped→`llm`, `nanobuds` narrowed to just nanobuds, `noxrr` split from the
new `nh3-synthesis`, keyword substring-traps pruned (`tool use`, `agentic`).

- **Tiers.** 0: free keyword/substring screen over title+abstract per topic —
  a paper matching nothing skips the LLM call entirely. 1: local cheap model
  confirms/expands the candidate set against the full topic list —
  **multi-label** (a paper may get 0, 1, or several `topic:` tags; genuinely
  cross-cutting papers are expected). Tier 2 (escalate on low confidence) is
  **not yet implemented** — open question in ADR 0060.
- **Writes** `Tag.open("topic:<slug>")` per confirmed topic (join key already
  existed — `precis-paper-tag-axes`) + a `Tag.closed("TOPICCASCADE",
  <CLASSIFY_TOPICS_VERSION>)` marker (written regardless of outcome,
  including zero matches) so a processed paper isn't re-claimed. No lease
  table (unlike the chunk cascade) — existence of the marker tag is the
  'done' check, mirroring `paper_glossary`; the paper corpus is small enough
  that a claims table isn't needed.
- **Pass.** `workers/classify_topics.py` `run_classify_topics_pass`,
  registered in `cli/worker.py` **default-OFF**
  (`PRECIS_CLASSIFY_TOPICS_ENABLED=1` / `--only classify_topics`).
  `tests/test_classify_topics.py`.
- **Per-topic gating (ADR 0068).** Each topic has its own `service_config`
  service `topic:<slug>` (independently flippable from `/categorizers`, no
  more "toggling one flips them all") — consulted *inside* the one pass to
  filter `_load_topics()` to the enabled subset (topics don't register their
  own passes: one pass, one LLM call/paper). The done-marker becomes
  `TOPICCASCADE:<version>-<hash(sorted enabled slugs)>`
  (`topic_marker_value()`, shared by the worker and the
  `src/precis_web/routes/categorizers.py` route) — a change to the enabled
  set changes the marker, lazily re-claiming the corpus (the backfill
  mechanism, replacing hand-bumping `CLASSIFY_TOPICS_VERSION` alone).
  `classify_topics` itself remains a global kill-switch: gate `default_on` =
  "any `topic:<slug>` enabled"; an explicit `classify_topics` row overrides.
- **Not yet built**: the quest-family synthesis tick body (harvest
  `topic:X`-tagged papers lacking an `integrated-into` link → merge into the
  topic's dossier `draft`), the weekly digest cast, and the daily-brief lane.
  Backlog: `OPEN-ITEMS.md` § "Topic dossiers (ADR 0060)". Full design:
  `docs/decisions/0060-topic-dossiers.md` + `docs/design/topic-dossiers.md`.

## Taproot canonicalization (Phase 1)

Design: `docs/proposals/taproot.md` (the full evidence-graph spec, phased
build); build ticket: `docs/proposals/taproot-phase1-canonicalization.md`.
Phase 1 is the flat claim canonicalizer everything else in the spec gates
on — **pure, no persistence, no migration** (a future phase's tags+links
overlay on `finding`/`ref_tags`/`links` — no schema of its own).

- **Package**: `src/precis/taproot/canon.py` — four functions, a cascade
  mirroring the ADR-0047 classifier (cheap-and-wide → escalate the risky
  bit): `extract_claim` (SMALL/local; chunk → `CanonicalClaim` sentence +
  light scope, or `None` on a pure-pointer chunk — the `NO-CLAIM`
  outcome) → `block` (no model; ANN over the `TAPROOT:claim`-tagged
  `finding` card embeddings, bge-m3, `k` nearest) → `dedup_judge` (MEDIUM;
  THE crux — one bounded pairwise `same`/`different`/`contradicts` call,
  biased hard toward `different`) → `place` (deterministic branching; a
  low-confidence `same` escalates to `merge_confirm`, BIG-tier, and a
  merge that still isn't confidently confirmed comes back `needs_review`
  rather than auto-attaching — over-merge is the one dangerous
  direction). Every call routes through `precis.utils.llm.router`
  (`source="taproot:extract"` / `"taproot:dedup"` / `"taproot:merge-confirm"`).
- **`TAPROOT` classifier** (Phase-2 slice 2a, taproot.md open #11) — the
  discriminator that tags `finding` rows `TAPROOT:claim` (grounded
  world-claim, the taproot hub) vs `TAPROOT:review` (editorial note,
  excluded from the claim graph). Built declaratively as a ref-level axis
  `src/precis/data/axes/taproot.yaml` driven by the generic
  `workers/axis_pass.py` runner (no bespoke worker code); auto-registers
  as the default-OFF `axis:taproot` service (`discover_axis_ids`), opt-in via
  `PRECIS_AXES_ENABLED` / `/categorizers`. `TAPROOT` is a registered closed
  axis (`store/types.py::_CLOSED_VOCAB`) so `search(kind='finding',
  tags=['TAPROOT:claim'])` filters; `finding` stays unlisted in
  `_KIND_ALLOWED_AXES` (unrestricted). Fail-open: `taproot.yaml` omits
  `default_unknown`, so an ambiguous read is `failed`/re-claimable, never a
  mis-tag. `canon.block` reads `TAPROOT:claim` hubs, so this makes live
  canonicalization real. Constants `TAPROOT_NAMESPACE`/`TAPROOT_CLAIM`/
  `TAPROOT_REVIEW` live in `canon.py`; `block` still degrades correctly with
  no tagged hubs (empty → brand-new claim). Corpus batch is a deliberate
  later run, not shipped on. Build ticket:
  `docs/proposals/taproot-phase2-hub-node.md`.
- **Eval**: `src/precis/taproot/eval_canon.py` (`eval_canonicalization`) runs
  `dedup_judge` over `tests/fixtures/taproot/claim_pairs.jsonl` (238
  pairs) and grades the fixture's 5-relation labels collapsed onto
  `same`/`different`/`contradicts`; prints the 3×3 confusion + over/under-
  merge rates. Gate: **over-merge (a false `same`) must be zero** —
  under-merge is tolerated (the safe direction). `tests/test_taproot_eval_canon.py`
  is the live-model harness test, skipped unless `PRECIS_TAPROOT_LIVE_EVAL=1`
  (never runs in the offline gate — it makes ~238 real LLM calls).
  `tests/test_taproot_canon.py` is the offline unit suite (mocked
  dispatch): `place` branching, `NO-CLAIM` detection, label-collapse.
  Gate validated 2026-07-29: **over-merge = 0 / 238** (under-merge 21.4%,
  tolerated), reached via a `_DEDUP_PROMPT` carve-out treating a specific
  quantitative formula/value/mechanism as *narrower* than the qualitative
  principle it instantiates (the pair-113 genus-species over-merge).
  `eval_canonicalization` streams a flushed per-pair line to stderr so the
  ~40-min live run is observable; run host-native (the dev container's
  `claude` CLI is unauthed and silently degrades every judgment to
  `different`).
- **Phase 2 (in progress)** — build ticket
  `docs/proposals/taproot-phase2-hub-node.md` (5 slices 2a–2e).
  - **2a (TAPROOT classifier)** — built (above).
  - **2b (evidence vocab + hub write-path)** — built (ADR 0073). One new
    link relation `establishes` (paper → `TAPROOT:claim` hub; originator),
    migration `0094`, seeded **without an inverse** — the hub reads evidence
    via `links_for(direction='in', relation=…)`. The other two roles reuse
    existing slugs (`corroborates` 0085, `contradicts` 0001); endpoint kinds
    disambiguate. Single write door `src/precis/taproot/hub.py`
    (`mint_hub` → `TAPROOT:claim` `finding`; `attach_evidence` →
    `paper --role--> hub`, role-and-target-guarded; `apply_placement` routes
    `canon.place()`, `needs_review` → `kind='todo'`, never auto-attach).
    Evidence role is *derived* later (seniority); attached `corroborates` by
    default. Edge `meta` shape defined; **populated by `chase` — see Phase 3
    slice W1, below**. Unit-tested here (`tests/test_taproot_hub.py`).
    **Source-side chunk grounding**: `attach_evidence` resolves the edge's
    `meta.source_handle` (a `pc<id>` handle or `slug~ord`) to the grounding
    chunk via `hub.py::_grounding_chunk_ord` and stores it as the edge's
    `src_chunk_id`, so the edge cites the supporting *passage* (`pc<id>`), not
    just the paper (`pa<id>`); two passages of one paper are two edges. Absent
    a resolvable handle it stays ref-level. `seed_claim_hub`'s dedup key
    includes the grounding chunk (so passages don't collapse); the CLI mint
    reports an `ungrounded` count nudging authors to supply `source_handle`.
  - **2c (seniority derivation + evidence view)** — built. Pure read/derive
    module `src/precis/taproot/seniority.py::derive_evidence` (no writes):
    reads the hub's inbound `establishes`/`corroborates` edges as supporter
    set S, walks `cites` edges *among S only* to find originators (a
    supporter some *other* supporter cites), derives
    `establishes`/`corroborates` **independent of the stored role slug**,
    orders each group by `refs.year` asc (NULL last). Locked decision:
    **no intra-S `cites` edges held → every supporter stays `corroborates`**
    (never guess an originator); `HubEvidence.coverage_note` flags the
    gap. The S2-global-citation-count fallback (taproot.md) is deferred to
    Phase 3, not built here. `contradicts` edges form a separate group,
    never folded into the split. Rendered via `finding`
    `get(view='evidence')` (`handlers/finding.py::FindingHandler.get`
    overrides the base — deliberately **not** added to `_numeric_ref.py`'s
    `_BASE_VIEWS`, so no other numeric-ref kind picks it up): three
    tables (originators/corroborators/contradicts), originator mark,
    support/integrity/caveats columns, a Phase-3 placeholder note when no
    edge carries `support` yet. Tests: `tests/test_taproot_seniority.py`.
  - **A1 (`\cite`→originators export, "living citation")** — built.
    `cli/resolve.py::_resolve_text` detects a `[pub_id]` placeholder that
    resolves to a `TAPROOT:claim` hub (`_lookup_finding`'s `is_hub` flag,
    an `EXISTS` over `ref_tags`) and, instead of the ordinary
    `primary_cite_key` substitution, calls
    `seniority.derive_evidence(store, hub_ref_id)` fresh on every run and
    resolves cite_keys via `Store.ref_cite_keys` (oldest alias): derived
    `establishes` originators first; if none have a cite_key (or none are
    derived yet — `coverage_note`), falls back to `corroborators`
    (best-available, flagged in the stderr diagnostic); a paper with no
    `cite_key` alias at all is skipped (with a warning) rather than
    failing the whole hub; no supporters/keys at all → in-flight, same
    `--strict` exit-3 gate as an ordinary tracing finding. Multi-originator
    hubs render a multi-key cite: `\cite{a,b}` (LaTeX, comma-joined) /
    `[a; b]` (plain/markdown). Because the split is recomputed per run
    rather than cached, a later-discovered originator or a hub merge
    improves the `.bib` output on the *next* `resolve` — no manual re-cite.
    Tests: `tests/cli/test_resolve.py`.
  - **Draft-export wiring, Phase 1 (living citations reach `kind='draft'`
    export)** — built. The A1 resolution policy (`_cite_keys_for_group` /
    `hub_cite_keys`, plus a `finding_cite_keys(store, ref_id) ->
    FindingCite` entry point) moved out of `cli/resolve.py` into
    `src/precis/taproot/cite.py` — the ONE module both `precis resolve`
    and the draft exporters call, so the two surfaces can't diverge again.
    `seniority.py` grew a public `is_claim_hub` wrapper (over the existing
    private `_is_claim_hub`) for `cite.py` to call rather than reaching
    into the private name. `export/latex.py::_render_finding_cite` and
    `export/docx.py::_finding_cite_keys_pinned` resolve via
    `finding_cite_keys` instead of a bare `primary_cite_key or pub_id`
    lookup: a `[fi<id>]` handle resolving to a `TAPROOT:claim` hub cites
    its currently derived `establishes` originator(s) (falling back to
    corroborators, then in-flight — no cite), recomputed on every export,
    exactly like `resolve`. A single resolved key stays on the pre-existing
    single-cite path byte-for-byte (zero regression risk for an ordinary
    finding); multiple keys render `\cite{k1,k2}` in LaTeX (a new
    `_cite_keys` helper, falling back to per-key `_cite` concatenation in
    patent/footnote mode) and one numbered `[n]` mark per key in docx
    (which has no multi-key literal).
  - **Draft-export wiring, Phase 2 (A2 pins reach `kind='draft'`
    export)** — built. `utils/mentions.py::BARE_BRACKET_REF_PATTERN` grew
    an additive optional `pin` capture (`[fi<id>>pa5,pc9]` replace /
    `[fi<id>+pa5]` supplement) alongside the unchanged `bare` handle group
    — every existing `bare`-only consumer (autolinker, web `linkify`) is
    byte-for-byte unaffected; `parse_pin_suffix` decodes it to `(op,
    handles)`. `cli/resolve.py`'s `_resolve_pin_handle` / `_apply_pin` moved
    into `taproot/cite.py` as `resolve_pin_handle` / `apply_pin` (returning
    a `PinResult` instead of mutating `resolve.py`'s `_Summary`) — the ONE
    pin-application policy now shared by `precis resolve`'s base32-token
    grammar and the draft `mentions` grammar, so a pin behaves identically
    wherever an author writes it; `FindingCite` grew an `evidence` field
    (populated for a hub) so a caller can `apply_pin` without re-deriving.
    `export/latex.py::_render_finding_cite` / `export/docx.py`'s pinned
    finding path read the `pin` group, thread it through `apply_pin`, and
    fold `PinResult.warnings` / a replace-divergence advisory into the
    exporter's `ctx.warnings`; a pin on a non-hub finding is dropped with a
    warning (a plain finding has no derived-originator set to override).
    Tests: `tests/test_taproot_cite.py`, `tests/test_mentions.py`,
    `tests/test_export_latex.py`, `tests/test_export_docx.py`,
    `tests/precis_web/test_linkify.py`, `tests/cli/test_resolve.py`
    (behavior-preserving refactor — pin CLI tests unchanged).
  - Not yet built: citation-card dedup (2d).
- **Phase 3 (in progress)** — forward `chase` wiring; slices land
  independently, W1 first.
  - **W1 (chase forward bridge)** — built. Default-OFF env flag
    `PRECIS_TAPROOT_CHASE_ENABLED`, independent of `PRECIS_CHASE_LLM` — the
    bridge fires only when it's on *and* the chase LLM verdict
    (`with_llm=True`'s `verification`) is available, so the deterministic
    chase path is untouched either way. On a finding's established-terminal
    hop (`workers/chase.py::_taproot_bridge`, called from
    `advance_finding` right after `_snapshot_chain`), builds a
    `CanonicalClaim` directly from the finding's own title + `meta.scope`
    (no `extract_claim` call — a chase finding is already a user-asserted
    claim, not untrusted chunk text; an empty title is the analogous
    NO-CLAIM skip) and runs it through `canon.block` → `dedup_judge` →
    `place` → `hub.apply_placement`. Skips entirely (no hub, no edge, no
    canon LLM calls) on `verification["supports"] == "no"` (NO-SUPPORT) or
    a missing embedder (`canon.block` needs `.embed_one` — a construction
    failure degrades to a logged no-op, never a chase crash).
    Terminal-hop `verification` maps onto the evidence edge `meta`:
    `support`/`support_reason` verbatim, `caveats` via the same
    `_aggregate_caveats` helper `_snapshot_chain` uses for `meta.caveats`
    (chain-wide, deduped), `source_handle=f"{paper_slug}~{chunk_ord}"`,
    `char_offset=None` (no producer yet). `hub.apply_placement` grew a
    `conn=` param (threaded from `mint_hub`/`attach_evidence`) so the
    hub/edge write lands in the SAME transaction as chase's
    `STATUS:established` flip; the bridge additionally wraps that write in
    a psycopg nested-transaction (savepoint) so a taproot-side failure
    rolls back only the taproot write, never the established flip. A
    `needs_review` placement files a minimal `kind='todo'`
    (`_file_taproot_review_todo`). Idempotent by construction: a
    re-established finding's `block` call finds the already-minted hub
    (same claim text → same ANN hit) and `place` returns `attach`, not a
    second `mint_hub`; `attach_evidence`'s underlying `store.add_link` is
    itself a no-op on a repeat `(src, dst, relation)` edge. The embedder
    for `canon.block` is threaded from `cli/worker.py`'s `_chase_pass`
    setup — reused from an already-booted `EmbedHandler` when one exists
    (no extra model load), else constructed once (only when the flag is
    on) and degraded to `None` on failure. A hub mints `STATUS:canonical`
    (not `STATUS:tracing`) — off the chase-status lifecycle entirely, so
    `chase`'s own claim query no longer re-picks it up (closes gripe
    175806's `STATUS` vocabulary overlap); `claim_tracing_findings`' explicit
    `TAPROOT:claim` exclusion stays as a defensive belt-and-suspenders
    guard. `FindingHandler.search`'s default (`status is None`) cohort
    (`_search_default_cohort`) unions `TAPROOT:claim` hubs in alongside
    `STATUS:established` findings — keyed on the tag, so hubs surface in any
    default search without an explicit `status=`; an explicit `status=`
    stays an exact single-status filter. Tests:
    `tests/test_taproot_chase_bridge.py` (mapping, NO-SUPPORT/NO-CLAIM/
    flag-off skips, no-embedder degrade, re-establish idempotency).
  - **A2 (authorial cite pinning)** — built (ADR 0074, closes taproot.md
    open #4 alongside A1). A1's living default stays the default; an author
    can pin a hub cite inline, syntactically, no storage:
    `[<pub_id>>pa5,pc293]` (**replace** — cite exactly these handles,
    ignoring the derived `establishes` set) / `[<pub_id>+pa5]`
    (**supplement** — derived originators plus these, deduped).
    `pub_id_lookup.py::PLACEHOLDER_RE` grew an optional trailing
    `(op)(comma-handle-list)` group both `resolve` and the reference ring
    parse (a bare `[pub_id]` still matches with the group `None` —
    unchanged pre-A2 grammar). A `pc<id>` (paper-chunk/passage) handle
    resolves to its parent paper — `.bib` is paper-level, the passage
    granularity just records *where* the author read the grounding.
    `cli/resolve.py::_resolve_pin_handle` reuses `Store.resolve_handle` +
    `Store.ref_cite_keys` (no hand-rolled chunk-to-paper lookup). Both
    `resolve` and the ring decode the token (pub_id + optional pin) via
    the one shared `pub_id_lookup.py::parse_pin`. A **replace** pin
    diverging from the hub's *currently* derived `establishes` originator
    fires a stderr advisory (`resolve: [<pub_id>] pinned {pa5} but derived
    originator is {pa99} — reconsider`); `--strict-pins` (mirrors
    `--strict-verified`) turns that into a CI-gate exit 3. A **supplement**
    pin is purely additive (derived plus these) and never diverges — no
    advisory, never trips `--strict-pins`. An unresolvable pinned handle
    warns + is skipped; an emptied `>`-replace pin falls
    through to the normal hub resolution rather than dropping the citation;
    a pin on a non-hub finding is meaningless and is ignored with a
    warning. `utils/refeye.py`'s Claims group reflects the same pin at read
    time (non-blocking, best-effort): the pinned paper renders a `📌`
    marker wherever it appears (including as an extra line when it isn't
    part of the derived evidence at all), plus a `(pinned; derived: pa99)`
    note on divergence. Tests: `tests/cli/test_resolve.py`,
    `tests/test_refeye.py`.
  - **W2 (per-hop corroborators)** — built. Same gate/flag as W1, same
    savepoint. After W1's terminal attach resolves a `hub_ref_id`,
    `_taproot_bridge` also walks every INTERMEDIATE `meta.chain` hop
    (everything but the terminal `chain[-1]`) and attaches each one that's
    a live `kind='paper'` ref as a `corroborates` evidence edge on the SAME
    hub (`_attach_intermediate_corroborators`) — so the hub actually gets a
    multi-supporter set for `seniority.derive_evidence` to split, instead of
    W1's single-supporter case where the split never exercised. Meta mapping
    is shared with W1 via `_evidence_edge_meta(verification, chain, slug=,
    ord_=)`: `caveats` is always the whole-chain aggregate
    (`_aggregate_caveats`), `source_handle` is per-hop. A hop is skipped
    when it's a dup (terminal/hub/an earlier hop — de-dup set, on top of
    `add_link`'s own `ON CONFLICT` no-op), isn't a live paper (defensive),
    or its own `verification["supports"] == "no"` (NO-SUPPORT, mirrors the
    terminal skip). A hop with no `verification` at all (never LLM-verified)
    still attaches — as a bare corroborator with `support`/`support_reason`
    left `None` rather than fabricating a verdict; role is always written
    `corroborates` — seniority derives `establishes` at *read* time from the
    `cites` graph, W2 never guesses it at write time. Tests (same file):
    multi-hop attach + a seeded `cites` edge exercising the real
    originator/corroborator split, NO-SUPPORT skip, non-paper skip, no-
    verification bare-corroborator attach, re-establish idempotency.
  - **A3 (authoring on-ramp — cite-seeded hub mint + draft write-hint)**
    — built. A human/backfill door that mints a claim hub from a claim +
    its already-known supporting paper cites, *without* the corpus chase:
    `taproot/authoring.py::seed_claim_hub` wraps the existing
    `mint_hub`/`attach_evidence` (no new write path), resolving supporter
    handles via the shared `resolve_handle_target`/`resolve_handle_ref`
    and kind-gating them to `paper`/`patent` — with a matching src-kind
    backstop added to `hub.py::attach_evidence`, so a typo'd spec handle
    (`dc<id>`/`me<id>`/a bare id) can't write a non-paper evidence edge
    and breach the paper-sourced invariant (open #15). Exposed as
    `precis taproot mint` (`cli/taproot.py` — `--spec`/`--json`,
    `--dry-run` with full pre-flight resolution so a bad handle aborts
    before any write, per-supporter attached/already/collapsed reporting).
    Roles are written `corroborates`; seniority still derives
    `establishes` at read time (W1/W2 unchanged). **Also exposed on the
    MCP surface** (`handlers/finding.py`, not just `cli/taproot.py`):
    `put(kind='finding', supporters=[…])` (bimodal with the ordinary
    `cited_in=` chase-finding put) mints/converges the same way, and
    `link(kind='finding', id='fi<hub>', rel='establishes'|'corroborates'
    |'contradicts', target=<pc/pa handle>)` attaches evidence to an
    *existing* hub (no CLI equivalent) — both route through the one
    write door (`taproot/hub.py`). Skill: `precis-taproot-help`. The
    write side gains a
    nudge: `handlers/draft.py::_pc_cite_claim_hub_hint` (backed by
    `taproot/lookup.py::hubs_grounded_by_paper` — one bounded
    `links`→`ref_tags`→`ref_identifiers` query) fires on a draft
    `put`/`edit` whenever a cited `[pc<id>]`/`[pa<id>]`'s paper already
    grounds ≥1 hub, listing each `[pub_id]` (a paper can ground several —
    the many-to-many case) so the author can switch to the living cite;
    `_hygiene_lines` gains a draft-level "N of M cited passages have a
    hub" scoreboard. Discoverability: new `precis-taproot-help` skill,
    wired into `precis-overview`/`precis-toolpath-help` + the
    draft/finding/fisheye/citation See-also blocks. Tests:
    `tests/test_taproot_authoring.py`, `tests/test_taproot_cite_hint.py`.
  - **Claim→claim `refines` links (link-don't-merge)** — built (migration
    `0100`, ADR 0073 amendment). A sharper/reworded version of an existing
    claim is minted as its OWN hub (own pub_id/`fi<id>`) and linked to the
    original with a directed `refines` edge (sharper → coarser), **not**
    merged into it — so both wordings stay citable and the next editor can
    choose. **Advisory only, no evidence flow**: each hub keeps its own
    paper→hub evidence; `refines` carries none. Single write door
    `taproot/hub.py::link_claims` (sibling of `attach_evidence`): guards both
    endpoints are live `TAPROOT:claim` hubs, `from != to`, relation in
    `CLAIM_LINK_RELATIONS`, FK pre-flight, idempotent. No inverse slug — read
    both directions via direct src/dst SQL (`seniority.py::derive_refines`),
    dodging the `links_for` inverse-rewrite trap. Surfaced read-only in the
    fisheye Claims ring (`refeye.py::_claim_links_lines`): each cited hub gets
    `↰ refined by fi<id> — <sentence>` (a sharper version exists) + `↳ refines
    fi<id> — …` (what it sharpens), capped. Authored via
    `link(kind='finding', id='fi<sharper>', rel='refines', target='fi<coarser>')`
    or `precis taproot refine --from <fi/pub_id> --to <fi/pub_id>`
    (`--dry-run`), both resolving through `authoring.py::resolve_hub_ref_id`. Tests:
    `tests/test_taproot_hub.py`, `tests/test_taproot_seniority.py`,
    `tests/test_refeye.py`, `tests/test_taproot_authoring.py`.
  - **Whole-draft `[pc<id>]`→`[fi<id>]` backfill** — built
    (`taproot/backfill.py`, driven by the **`taproot_backfill` job type**
    (`workers/job_types/taproot_backfill.py`, `claude_inproc` lane on melchior)
    — enqueued `put(kind='job', job_type='taproot_backfill', params={scope,
    ref_level})`, scope = slug/section/leaf, run **serial + checkpointed**
    (`meta.done_chunk_ids` resumes a re-claim) — or the CLI `precis taproot
    backfill --chunk dc<id>` / `--draft <slug>` (`--apply`/`--ref-level`); both
    call the same `plan_chunk`/`apply_chunk`. **The LLM cascade runs on the
    cluster worker, never in the MCP** — the MCP only mints the job (no
    `dispatch()` in a handler). Walks a draft chunk's
    legacy bare `[pc<id>]` cites, **cite-group anchored** (the pc markers
    partition the prose into grounded spans — not a sentence split; adjacent
    `[pc1][pc2]` collapse to one hub with two evidence edges → one
    `[fi<hub>]`), and runs each claim through the **same** cascade as the
    forward bridge (`extract_claim → block → dedup_judge → place →
    apply_placement`, cascade fns injected for tests) so it **converges onto
    an existing hub** instead of the `pub_id`-only byte-match of
    `seed_claim_hub`; `apply_chunk` re-derives per-group so a later group's
    ANN sees earlier in-chunk mints. `needs_review` files a review `todo` and
    leaves the `[pc…]`; a no-claim / unresolvable span is left as-is. Prose
    rewrite goes through the draft edit door (DELETE+INSERT, embeddings
    re-run), never a raw UPDATE. **On-demand, one chunk/draft at a time — not
    a corpus sweep** (194 unconverted chunks as of build; the corpus-wide
    converging pass stays a future worker, reconciled with hub-refine).
    Idempotent at the draft level. Edges carry `meta.origin='draft-backfill'`
    (fingerprint) under the seeded `set_by='agent'` actor — distinct from the
    chase pilot's `set_by='chase'`, all filling the same graph. Tests:
    `tests/test_taproot_backfill.py`.
  - **`[pa<id>]` arm (whole-paper cites) — slices 1+2 built**
    (`taproot/backfill.py`, same `taproot_backfill` job (`params.ref_level`) /
    `precis taproot backfill [--ref-level]` door as the `[pc]` arm above,
    `docs/proposals/taproot-draft-pa-arm.md`). The segmenter also anchors
    on bare `[pa<id>]` groups (kept **separate** from `[pc]` groups — a
    kind-switch breaks contiguity, so a whole-paper and a passage cite never
    fold together), classified by the cited paper's `count_blocks`: a **stub**
    `[pa]` (0 blocks, un-fetched) → `stub-fetch-first`, skipped, no write
    (never cite an unread paper as evidence); a **fetched** `[pa]` →
    **`reground`** by default (slice 2): an injectable `LocateFn` (default =
    lexical unigram pick + Tier.MEDIUM `_chase_llm._locate_chunk_in_target`
    confirm — the arm's one LLM call, write/dry-run path only) picks the
    grounding passage and the `[pa<id>]` run rewrites to `[pc<chunk>]` (no hub —
    a cite refinement the existing `[pc]` path promotes on a later run,
    chunk-grounded; `reground-nomatch` when no passage is found, **all-or-nothing**
    per contiguous run so a partial rewrite never erases a token). The explicit
    `--ref-level` override instead promotes **ref-level** (ungrounded, no
    grounding passage — `edge.meta.arm='pa'`, `n_ungrounded` surfaced), rewriting
    `[pa]`→`[fi<hub>]` and inheriting `apply_chunk`'s idempotent re-convergence.
  - Not yet built: further chase slices (S2-global-citation-count
    fallback promotion, integrity axis (Phase 4), corpus-wide backfill
    *sweep* (a worker pass — the per-chunk/section/draft doors above are
    its manual predecessor)); `refines` evidence-flow (v1 is
    advisory-only).
- **Hub-refine (converging corroborator enrichment, claimed off a due-set)** —
  built, dark (`PRECIS_TAPROOT_REFINE_ENABLED`, default-OFF like every
  taproot flag). Build ticket: `docs/proposals/taproot-hub-refine.md` (+ its
  2026-08-01 addendum). Closes a gap W1/W2/A3 leave open: evidence attaches
  to a hub only as a *side effect* of chasing a finding or a human minting
  (`put(kind='finding', supporters=…)` / `precis taproot mint`) or manually
  attaching (`link(kind='finding', rel=…)`) — nothing ever revisits an
  **existing** hub and asks what else in the corpus supports it. New pass
  `src/precis/workers/hub_refine.py::run_hub_refine_pass`, wired into
  `cli/worker.py` beside `inbound_chase`
  (`workers/registry.py::ServiceSpec` `name="hub_refine"`,
  `doc_skill="precis-taproot-help"`). Per pass:
  `_claim_hubs_due_for_refine` claims up to `PRECIS_TAPROOT_REFINE_HUBS_PER_PASS`
  (default 8) `TAPROOT:claim`/`STATUS:canonical` findings that are **due**
  (`hub_refine.py::_is_hub_due`) — carry a `TAPROOT_DUE` ref tag (set by the
  `chase_trigger` pass below), are never-refined, are a sha-reopen
  (`meta.last_refined_sha != taproot/canon.py::claim_sha(title)`, an edited
  claim), or have crossed the `PRECIS_TAPROOT_REFINE_BACKSTOP_H` failsafe
  (default 2160h/90d — a stuck-row backstop, not a schedule; **replaces** the
  old `PRECIS_TAPROOT_REFINE_INTERVAL_H` weekly cadence) — never-refined
  first, oldest `last_refined_at` next, `SKIP LOCKED`, popping each claimed
  hub's `TAPROOT_DUE` tag as it claims; a sha-reopen also clears
  `meta.taproot_rejected` before discovery. `_refine_one_hub` embeds the
  hub's claim sentence (`utils/embed_query.py::embed_query`) and runs
  `store.search_blocks(mode="semantic", kind="paper")` for the top-K
  (`PRECIS_TAPROOT_REFINE_TOPK`, default 8) candidate paper chunks, skips a
  candidate already carrying a `corroborates` edge on the hub
  (`taproot/authoring.py::_evidence_edge_exists`, checked **before** any LLM
  spend) or already recorded `no` in the hub's rejection memo
  (`finding.meta["taproot_rejected"]`), then verifies survivors with
  `workers/_chase_llm.py::_verify_support_with_caveats` — `yes`/`partial` →
  `taproot/hub.py::attach_evidence` (role `corroborates`); `no` → append to
  the rejection memo. `meta.last_refined_at`/`meta.last_refined_sha` are
  stamped unconditionally, even on an empty pass, so the due predicate's
  never-refined/never-reopened legs hold. Convergence is the whole point,
  never a periodic re-scan: idempotent attach + the pre-verify edge-exists
  check + the rejection memo + the due-set claim query together bound
  per-run LLM spend and let a saturated hub drop out of the claim query on
  its own. Grows only the *supporter* set — originator (★) derivation stays
  `seniority.py::derive_evidence`'s job, unchanged. No embedder wired → the
  whole pass logs a warning and no-ops (mirrors the forward bridge's own
  degrade). Tests: `tests/workers/test_hub_refine.py`.
- **Chase trigger (Phase 1 — incremental due-set watermark)** — built, dark
  (`PRECIS_TAPROOT_CHASE_TRIGGER_ENABLED`, default-OFF). New pass
  `src/precis/workers/chase_trigger.py::run_chase_trigger_pass`
  (`workers/registry.py::ServiceSpec` `name="chase_trigger"`), the
  ingest-triggered watermark hub-refine's build ticket originally deferred
  as out-of-scope. Indexes claim hubs into a new `claim_embeddings` table
  (migration `0101_taproot_claim_embeddings.sql`: one vector per
  `(claim_ref_id, embedder)`, `claim_sha`-gated re-embed off
  `taproot/canon.py::claim_sha` — the same helper hub-refine's sha-reopen
  check uses, so "changed" means the same thing to both passes), then
  sweeps embedded `paper`/`patent` body chunks not yet carrying a
  `CHASETRIG:<version>` chunk tag (the classify/classify_topics
  done-marker idiom — no lease table). Per swept chunk, a reverse ANN
  (chunk vector vs. the small ~1.2k-row `claim_embeddings` table, no ANN
  index — a flat `<=>` scan) against a cosine-distance floor
  (`PRECIS_TAPROOT_TRIGGER_MIN_SIM`, default 0.45, loose-biased —
  over-triggering is cheap since hub-refine still prechecks before any LLM
  spend) marks each near claim hub `TAPROOT_DUE`, then marks the chunk
  swept regardless of match (drains the queue, guarantees convergence;
  bump `CHASETRIG_VERSION` to force a corpus-wide re-sweep). No embedder →
  logs and no-ops (mirrors hub-refine's own degrade). Tests:
  `tests/workers/test_chase_trigger.py`.

## Other live affordances

One line per affordance — code path + skill for the detail. The
`precis-*-help` skills are the authoritative, on-demand reference (the MCP
serves them via `get(kind='skill', id=…)`); this list is just the index.
The master kinds table lives in the `precis-overview` skill.

- **Cluster maps (`/clusters`)** — spatial SOM browse over chunk embeddings;
  `clusterize` worker (`utils/cluster_map.py`, numpy-only, warm-started daily),
  `0027_clusterize.sql`, `precis_web/routes/clusters.py`.
- **`folder`** — single-parent placement container for authored artifacts on
  `refs.parent_id` (ADR 0045); `handlers/_placement.py`, `KindSpec.role`,
  `search(folder=)` scopes a subtree. Skill: `precis-folder-help`.
- **`material`** (`docs/proposals/materials-handbook-kind.md`, Phase-1) —
  CRC-handbook-style engineering material properties store, **v1
  canonical-units-only** (no `pint`/conversion/`units=`/off-sample estimate —
  deferred follow-ons). Star schema: the entity is a slug `refs` row
  (`kind='material'`, `handlers/material.py`); `material_properties` is a
  typed, growable registry (`core` curated by migration 0092 + `proposed`
  mintable at write time, must declare a canonical unit + dimension);
  `material_values` is the fact table — one row per sourced measurement
  (`value/conditions/maturity/source`), `material_ref_id` handler-enforced to
  `kind='material'` (`refs` has no per-kind FK). `put` with `property=`
  appends a value and rejects a non-canonical `unit=` (names the canonical
  one); `search(property=, min=, max=, maturity=)` is the range-filter read.
  No card/embedding — the handbook page is a SQL join, not a search. Handle
  `ma<id>`. Skill: `precis-material-help`.
- **`component`** (`docs/proposals/component-kind.md`) — general
  procurable-part store (bolts/hoses/pipes/beams/gaskets/bearings/
  adhesives/electronic parts), mirroring `material`'s star schema plus a
  **category dimension**. Entity is a slug `refs` row (`kind='component'`,
  `handlers/component.py`); `category=` required on create, resolves
  against `component_categories` (flat, `core` curated by migration 0093 +
  `proposed` mintable at write time); `component_specs` is the property-
  registry analogue with a nullable `category_id` (NULL = universal —
  `mass`/`unit_cost`/`length_overall` — non-null = scoped, handler-enforced
  applicability at write time); `component_spec_values` is the fact table,
  `component_ref_id` handler-enforced to `kind='component'`. An unknown
  numeric `spec=` mints a `proposed` spec scoped to the writing component's
  category. `made_of=` links to a `material` ref (`made-of`/`used-in`
  relation pair) — the substance-composition edge; effective-property
  inheritance over it is deferred, as is the `contains`/BOM structural-
  composition follow-on. `search(spec=, min=, max=, maturity=, category=)`
  is the range-filter read. Deliberately not `part` (the JLCPCB/LCSC
  ingest-only catalog). Handle `cp<id>`. Skill: `precis-component-help`.
- **`email`** — live, read-only IMAP mailbox browse (`handlers/email.py`,
  direct `Handler` — mirrors nothing, IMAP is source of truth). `precis.mail`
  = `account` (typed view over `email_account` row + JSONB config, provider
  presets, pluggable `password`/`xoauth2` auth) · `imap` (stdlib connect +
  probe) · `message` (list/fetch; `BODY.PEEK` + readonly SELECT ⇒ browsing
  never marks `\Seen`) · `inject` (`scan_tier0` — regex tier-0 injection scan,
  `clean`|`suspect` + named signals). `get(kind='email')` overview ·
  `id='INBOX'` folder · `id='INBOX/<uid>'` message · `account=` disambiguates.
  `mail_poll` (`workers/mail_poll.py`, **dark** behind `PRECIS_MAIL_POLL_ENABLED`)
  = per-account IMAP poll (cadence + backoff, watermark-adopt on first poll /
  resync, no back-fill) → inline tier-0 scan → verdict rows in `email_scan`
  (no body stored); `precis email poll` runs a tick by hand. Accounts via the
  `precis email` CLI; password in the vault (`email.<addr>.password`).
  `inject_scan` (`workers/inject_scan.py`, **dark** behind
  `PRECIS_INJECT_SCAN_ENABLED`) = the deep rung: lease tier-0 verdicts
  (`pending_email_scans`), re-fetch the body, model-score (tier 1,
  `DispatchClient`; escalate `suspect` to tier 2 when
  `PRECIS_INJECT_SCAN_ESCALATE_MODEL` set), guarded CAS `upgrade_email_scan`
  (`tier < new_tier`, the lock-free claim), `raise_alert` on `high`. The browse
  handler badges listings (🚫/⚠) and **withholds** a `high` body (metadata
  only). `precis email poll` / `precis email scan` run a tick by hand.
  Migrations `0075_email_account` + `0076_email_scan` (slice 4 needed none);
  design `docs/design/email-kind.md`. **Slices 1–4 (config + browse +
  poll/tier-0 + inject_scan/quarantine) live (3 & 4 dark behind their flags);
  promotion + brief (slice 5) and send are later — v1 is read-only.**
- **`plan`** — a thread's reasoning outline (ADR 0051 §2b, slice A1): a
  hierarchical todo-list + notes on the `draft` chunk-tree substrate
  (`handlers/plan.py`, reusing the kind-parameterized `DraftMixin`), but a
  **distinct kind that is never exported** (`export/guard_exportable`,
  `corpus_role='none'`). Rendered whole with `[open]`/`[wip]`/`done:` +
  `?`/`⚠` + a model-owned `▸` cursor (`meta.cursor` on the ref); nodes
  `pe<id>`, one per project via `plan-of`. Migration `0056_plan_kind.sql`.
  Ships dark — nothing dispatches to it yet.
- **`figure`** — an interactive **SVG canvas you draw *with* the model**
  (`handlers/figure.py` + `precis/figure/{svg,turn}.py`, reusing the
  kind-parameterized `DraftMixin`), a **distinct kind that is never exported**
  (`corpus_role='none'`). Three model-owned docs — the SVG source (`figure_node`
  chunk `fn<id>`, `meta.no_index` so raw markup never embeds), a **shared
  vocabulary** (`figure_vocab`, embedded — high-level, human-facing), and
  **implementation notes** (`figure_notes`, `no_index` — the model's private
  design log; migration 0058) — plus a `figure_turn` chat log. Vocab/notes are
  born empty (the "what this doc is for" seed is instruction, kept in the
  prompt/`precis-figure-svg` skill, never stored as content). The pinned
  `precis-figure-svg` skill body is prepended to the turn prompt (editing the
  skill edits the prompt). The
  draw-with-me turn loop (`figure/turn.py`: state + two lints (compile +
  out-of-bounds) + vocab + user msg → whole-source rewrite, sanitize, bounded
  auto-heal) is the **web** editor `/figure` (`precis_web/routes/figure.py`);
  the canvas renders SVG as a script-safe `<img>`. MCP surface is
  put/get/edit/delete/link. Migration `0057_figure_kind.sql`; skills
  `precis-figure-help` + `precis-figure-svg`. Slice 1 = SVG 2D, browser-
  rendered; **deferred**: PNG/animated raster export, three.js/`scene3d` mode,
  per-node chunk split, draft-embedding, `read(handle)` reference tool.
  Since ADR 0057 `figure` is the **SVG instance** of a shared **diagram core**
  (`src/precis/diagram/` — the `DiagramLang` port + the generic turn loop /
  context assembler; `figure/turn.py`+`context.py` are thin shims), and its
  elements **bind to the chunks they depict** (see `mermaid` below).
- **`mermaid`** — a **mermaid diagram you draw *with* the model** (flowchart /
  sequence / state / class …), the **second instance** of the diagram core
  beside `figure` (ADR 0057, slice 4): same draw-with-me turn loop, three docs
  (`mermaid_node`/`mermaid_vocab`/`mermaid_notes` + `mermaid_turn`), same
  handle scheme (`mm<ref>`/`mn<chunk>`), never exported (`corpus_role='none'`).
  Validation / SVG render / PNG-PDF export are **pure-Python via `mermaidx`**
  (`src/precis/mermaid/mermaid.py::MERMAID_LANG` — the real mermaid.js in an
  embedded QuickJS + resvg; **no Node, no Chromium, no container**), lazy-
  imported behind the `[mermaid]` extra. **Element→chunk bindings (ADR 0057):**
  a node (by its stable id) binds to the `dc…`/`pc…`/`me…` chunk it depicts via
  a chunk-level `depicts` link (element id in `links.meta.elements`); the turn
  prepared-context lists each node + topology + the linked chunk body, and a
  `[binding]` lint catches drift. MCP `handlers/mermaid.py` (put/get/edit/
  delete/link) + web `/mermaid` (`precis_web/routes/mermaid.py`, renders
  server-side through figure's `sanitize_svg`). A **first-class kind**
  (registered like `figure`, no env gate); the `[mermaid]` extra provides the
  engine and is installed on the serve / web / worker hosts (a build without it
  degrades validation/render gracefully). Migration `0066_mermaid_kind.sql`; skills
  `precis-mermaid-help` + `precis-mermaid`. **Autonomous tick:** the
  `diagram_propose` job_type (`workers/job_types/diagram_propose.py`, ADR 0057
  slice 5) runs **one** figure/mermaid turn against the model from an
  instruction + seed chunk handles — mutating the diagram in place + reconciling
  bindings, owned by the diagram (compute lane; figure/mermaid set
  `KindSpec.can_own_jobs`). Deferred: a full mermaid source grammar (node
  extraction is a scan), rich cross-kind seed rendering.
- **`gripe`** — first-class bug tracker; body + comment timeline as chunks
  (`gripe_body`/`gripe_comment`), so they embed + keyword-index automatically.
- **`anki`** — spaced-repetition **cloze** cards (`{{c1::…}}`) that live in the
  corpus and sync to AnkiWeb. Numeric-ref `handlers/anki.py`; body is cloze
  markup, `meta` carries the generic Anki note shape (`notetype`/`deck`/`fields`,
  optional terse `Back Extra` after a lone `---`), emits a markup-stripped
  `card_combined` chunk so cards embed + search. **Anki owns scheduling — no
  SM-2.** Supersedes and retires `flashcard` (handle prefix `fc`→`ak`; migration
  0060). **Headless AnkiWeb sync** (`src/precis/anki/`, `precis anki-sync`, gated
  `PRECIS_ANKI_ENABLED`, `anki` wheel lazy-imported/ansible-installed): precis is
  the Anki client holding one `.anki2` mirror; add-only-own-notes by stable guid,
  guard allows FULL_DOWNLOAD but **refuses FULL_UPLOAD**, reads decay stats back
  into `meta.anki_stats`. **precis-fix** (`anki/fix.py`, `--fix`): tag a card
  `precis-fix` in Anki + a comment → LLM rewrites it → written back (per-card
  opt-in widening of own-notes-only). **Foreign-card read-only PG projection**
  (`anki/project.py`, `--project`): every Anki card (any notetype) mirrored into
  PG as a read-only `anki` ref (`meta.source=anki-foreign`), content-hash-gated so
  only changed cards re-embed (stats refreshed cheaply each sync), vanished ones
  soft-deleted — the whole collection searchable + feeding the knowledge-model,
  can't corrupt the account. **Per-card decks** (`deck-<topic>` tag →
  `Precis::<topic>` sub-deck). **Leech-finder** `get(kind='anki', id='/leeches')`
  surfaces bad-recall cards (high lapses / collapsed ease from `meta.anki_stats`)
  → fix-cloze-or-study. Design `docs/design/anki-integration.md`; skills
  `precis-anki-help` (ref) + `precis-cloze` (authoring craft).
- **`concept` + the reading-prep loop** — an adaptive, activity-driven study
  system that preps the human on what the corpus is working on (design-of-record
  `docs/design/reading-prep-loop.md`, **ships dark, in progress**). The spine is
  a bespoke **concept graph**: `kind='concept'` (numeric-ref `handlers/concept.py`,
  handle `cn`, migration 0063) is a node in the learner's knowledge graph — a term
  with a continuous **mastery** field + derived state + an embeddable
  `card_combined` definition (so a concept *is* a vector), and typed edges
  `has-prerequisite`/`prerequisite-of` (the learning DAG), `analogy-of`,
  `contrasts-with`, `represents`. Node model + promotion live in
  `src/precis/reading/` (`concepts.py`, `promote.py`). Slices built:
  **(1)** `paper_glossary` worker (`workers/paper_glossary.py`, default-OFF
  `PRECIS_PAPER_GLOSSARY_ENABLED`) — a per-paper inferred glossary as a
  `card_glossary` (ord=-1000) derived chunk; **(2a/b/c)** concept kind + graph
  relations + promotion (`reading/promote.py`: glossary terms → concept nodes,
  corpus-wide **name-anchored dedup** via `meta.norm_name`, cohort membership in
  `meta.cohorts`, `derived-from`→paper provenance). A **non-concept filter**
  (`reading/term_quality.py`, the `precis-cloze` rule-0 taxonomy) gates both the
  glossary build (`paper_glossary._clean_clusters`) and the promotion chokepoint
  (`promote_paper`, counted as `dropped`) so topic-labels / stock phrases /
  front-matter never become concepts (gripe 186183). Remaining: graph-edge
  inference, mastery-from-Anki, embedding routing (reading-readiness /
  shortest-path / **daily review-path walk**), booklet, cards-as-representations,
  briefing+audio. **Anki is a renderer, not the brain** — the concept graph is the
  source of truth; leaf cards sync down.
- **`quest`** — the striving above the work (design-of-record
  `docs/proposals/quest-layer.md`; slice 1 **live, read-only, does not steer
  yet**). A quest is a **perpetual, unachievable striving** (the medieval Grail
  sense) — the **only** new aim-kind (numeric-ref `handlers/quest.py`, handle
  `qu`, migration 0065, `emits_card` so it *is* a vector, `corpus_role='none'`).
  Never `done`: lifecycle `active|dormant|abandoned`, enforced in the handler's
  `tag()` (STATUS is a shared union axis, so the value-subset is guarded per-kind).
  Achievable work stays ordinary todos/projects marked `serves` → the quest — a
  **DAG of strivings** above the todo tree, walked by `view='tree'` (servers by
  kind + sub-quest recursion + deed ledger). Two records: an **append-only
  `quest_log` logbook** (the gripe body+comment pattern — WORM, dated, typed
  entries `note·observation·hypothesis·result·decision·dead-end·milestone·
  reflection·cost` + `by`; a `milestone` is a deed, `cost` feeds the **tote** =
  a query over the dated log, no separate cost store), and a dossier `draft`
  (arrives with the loop, slice 4). The dossier body is **two chunks** (ADR
  0064 §A, `quest/dossier.py`): a whole-rewritten **narrative** + a **pinned
  ledger** (`meta.pinned='ledger'`, generic `paragraph` + `patch_chunk_meta`,
  no migration) of strategic *tried/ruled-out/open* directions that survives
  every `rewrite_dossier` byte-identical and injects into the tick prompt as a
  "do NOT re-propose" constraint — so a rewrite that drops a rule-out can't
  silently re-open ruled-out ground (the autocatpath dead-3-days spin; distinct
  from the per-*candidate* `ruled-out:` structure tags). The model pins entries
  via a `ledger_add` output field; a pre-0064 dossier heals its ledger lazily
  (`ensure_ledger_chunk`). The dossier **owner is any process**, not just a quest
  (ADR 0064 §B, built 2026-07-24): `dossier.py`'s functions take `owner_id` (any
  ref) via the owner-agnostic `dossier-of` edge, so a non-quest living-review
  process can own — and export — one (migration-free; legacy `dossier_of_quest`
  meta resolves unchanged). **Slice 2 (reweighting) live**
  (`src/precis/quest/reweight.py`): priority flows down the `serves` DAG
  (max-agg, `STRIVING_DECAY` per quest→quest ladder hop; only **active** quests
  pull; canonical priority = `refs.prio`, set via a `PRIO:` tag synced in the
  handler) into three sinks — **rotation** (`_fetch_doable`/`render_roots`
  discount a strategic's picks by served weight), **acquisition** (`fetch_oa`
  claim tiers a quest-serving stub ahead), **reading**
  (`build_meditation(bias_active_quests=)`, dark until reading-prep slice 3). A
  **no-op until quests + servers exist**, so it's live without a flag. Coming:
  gap surfacing (slice 3), the autonomous research loop (local grind + frontier
  steering, materials as `structure` servers, slice 4). Skill:
  `precis-quest-help`. **Perpetual loop = a `coordinator` job**
  (`workers/job_types/quest_tick.py`): each slice harvests finished sims, runs
  the review+propose tick (`run_quest_tick`, tier `big` — routed by
  `resolve_chain`/`llm.chain.big`, a served local OSS model when the chain
  points there), co-dispatches the batch's barrier/relax sims, and `Yield`s on an
  `at_time` heartbeat until they land — event-driven, self-paced on sim
  completion (not a cron), with per-quest backpressure (no new batch while one is
  in flight) + a node-load starvation gate. The coordinator claim now honours
  `meta.params.target_node` (`coordinator._claim_jobs` passes `PRECIS_NODE`) so
  the loop pins to the GPU/model node; autocatpath sims carry `resources.wall_seconds`
  so a full reaction-network NEB can't lease-expire mid-run. A **failed/paused**
  tick (transient LLM 400/502, breaker/quota pause) backs off on the heartbeat and
  **retries** — a failure counts toward `PRECIS_QUEST_TICK_MAX_FAILURES` (default
  5 consecutive) after which the loop rests; a pause retries free. A
  *successful* tick that dispatches nothing splits on whether the model
  **engaged** (wrote logbook / rewrote dossier / proposed / pinned a ledger
  direction): an *engaged* dry tick is evidence of exhaustion →
  `PRECIS_QUEST_TICK_MAX_DRY` (3); a **punt** (produced nothing) is just a flaky
  slice → the larger `PRECIS_QUEST_TICK_MAX_PUNT` (8) before resting (ADR 0064
  §dry). **RC2 self-rest:** `_dispatch` checks the quest's liveness (the same
  `active_quest_ids` STATUS:active filter the reconciler uses) at the top of
  *every* slice and `Done(success=True)`s the moment the quest is non-active —
  so an awaiting loop winds down on its next heartbeat, not only via the dry
  budget (the reconciler still never cancels a loop). **Infra ≠ dry (ADR 0064
  §C):** a candidate whose relax failed `failure_class='infra'` is re-dispatched
  **once** (`meta.quest_infra_retries`) so it goes non-terminal and the loop
  *awaits* it instead of drifting dry; a 2nd infra failure files a
  `quest-infra-failure` gripe and stops — never ruled out (no physical verdict).
  The **barrier lane mirrors this** (§C completed): a failed `autocatpath_explore`
  is *always* a crashed NEB (a compute failure, never a physical "no pathway"
  verdict), so it retries once (`meta.quest_autocatpath_infra_retries`) then gripes
  (`lane="autocatpath"`) and **never** rules out — `_latest_autocatpath_job` +
  `dispatch_autocatpath` re-dispatch, same retry-once-then-gripe shape as relax.
  **Barrier quality gate** (`compute._pathway_quality`, gr172323): harvest
  lifts not only the scalar `barrier`/`span` but a trust verdict read from the
  linked pathway's `meta.warnings` — `barrier_trusted=False` iff any
  `NEB not converged` or adsorbate-`detached` warning. An untrusted barrier is
  **excluded from ranking** (dropped from `measures` in
  `frontier._candidate_from_structure` → the candidate lands `unevaluated`,
  never on the frontier; the raw value survives as
  `flags.barrier_untrusted_value` for the leaderboard's `⚠ non-converged` /
  `(excluded)` cell) and can **never graduate** (`graduate.py` belt-and-
  suspenders gate on `_AUTOCATPATH_GATED_KEYS`). NB single-seed runs make autocatpath's
  own `low_confidence` uninformative (always `n<2`), so the *warnings* are the
  signal, not that flag — the physical fix (endpoint desorption pre-flight,
  bigger NEB budget, seed ensemble) is autocatpath-side + quest-config, tracked in
  gr172323.
  **Stale-engine invalidation** (P0b): `_autocatpath_content_key` folds an
  engine-version token (`_autocatpath_engine_token` — env
  `PRECIS_AUTOCATPATH_VERSION`, else the `_AUTOCATPATH_CACHE_EPOCH` constant), so
  a new autocatpath deploy re-keys and re-scores instead of deduping onto stale
  completed jobs. This was the qu164903 empty-frontier trap: 21 candidates pinned
  on autocatpath 0.1.1's desorption false-positives (102 phantom `detached`
  warnings → all untrusted → `unevaluated`), never re-scored on the deployed 0.4.0
  (which relaxes the same geometries with 0 detached, trusted — confirmed on
  st165612). `compute.redispatch_candidates` (CLI `precis quest redispatch <id>`)
  re-dispatches every non-ruled-out candidate on the deployed engine. When an
  engine improvement invalidates not just the numbers but the *conclusions* drawn
  from them, `compute.reset_compute` (CLI `precis quest reset-compute <id>`) first
  surgically wipes the barrier-lane history — nulls stale measures/quality flags,
  drops `ruled-out:*` + `needs-experiment` tags decided on stale barriers, resets
  the dossier (the tick regenerates it from clean data so the discovery agent
  stops reasoning from confabulated conclusions) — keeping the candidate designs +
  papers; then `redispatch` re-scores.
  **Per-seed fan-out (§B-1, gr180096 — the spark wedge fix):**
  `dispatch_autocatpath` no longer mints one ~90-min in-process
  `autocatpath_explore` monolith (whole network x N seeds x full NEB,
  SIGTERM-deaf, the 81-starts/0-completions wedge). It mints a small job
  tree instead: one content-addressed `autocatpath_seed` job per
  `(mlip.specs() entry, search.seeds entry)` — idem-keyed on
  `sha(config, slab, seed, model_index, autocatpath_version)`, so a killed
  seed loses only that seed and a re-dispatch skips any seed whose todo
  already exists — plus one `autocatpath_aggregate` node that combines the
  seed partials in-process (`aggregate_seed_partials`, pure numpy). The
  aggregate is gated with **no new coordinator**: each seed job is wrapped
  in its own per-seed todo (`auto_check=child_job_succeeded`, minted
  synchronously by `dispatch_autocatpath`); the aggregate todo (`T_agg`,
  minted with `meta.executor`/`job_type` but no job of its own) only
  becomes a dispatch-worker candidate once every per-seed todo under it
  resolves — the existing "no live child todo" candidacy gate — at which
  point the dispatch worker mints `T_agg`'s own `autocatpath_aggregate`
  job the ordinary way. `autocatpath_explore` stays registered (legacy
  queued rows don't error-loop) but is never minted again;
  `_fresh_autocatpath_jobs` reads either shape. See
  `docs/proposals/gpu-priority.md` Phase 1 and
  `docs/design/autocatpath-integration.md` §3.8.
  Reaction (slab) candidates relax the box **in-plane** (`cell="inplane"`
  — a/b + γ free, c-axis/vacuum pinned) so stability is judged on a relaxed slab
  (`quest/compute.py`; the `relax` op's variable-cell mode in `structure/relax.py`).
  **Loop existence is reconciled, not allocated**: the old rung-4d
  `quest_dispatch` worker pass picked one active quest per cycle and ran an
  **inline** `run_quest_tick` (a single scored step) — replaced by
  `quest.loop.reconcile_quest_loops` (the `quest_loop_reconcile` agent-worker
  pass, same `PRECIS_QUEST_LOOP_ENABLED` gate): every cycle it cools stalled
  quests, then `ensure_quest_loop`s each remaining active quest — an
  idempotent mint (`idem_key=quest_tick:<id>`) that leaves a sleeping
  coordinator alone and re-arms one that rested. `quest.allocator` (the
  bandit/EWMA scoring + `pick_next_quest`) still backs the **manual** `precis
  quest run` CLI one-shot tick; it no longer drives the background loop.
  **Reboot self-heal (`_reap_orphaned_loop`)**: a coordinator slice killed
  mid-run (node reboot / worker restart) strands its job at a *non-terminal*
  status, and its `idem_key` then blocks the re-mint — before, only the sweeper
  freed it, after its ~1h stuck-job threshold (a manual `STATUS:cancelled` was
  the fast path). Each reconcile pass now first cancels a *provably orphaned*
  loop — non-terminal, `meta.lease_until` non-null and expired **beyond a grace
  margin** (`PRECIS_QUEST_LOOP_ORPHAN_GRACE_S`, default 600 s) — so
  `ensure_quest_loop` re-mints in the same pass (~15 min recovery). The grace
  margin is load-bearing: unlike the ~1h ssh_node lease, a coordinator lease is
  5 min and *not renewed mid-slice*, so a live-but-slow `big`-tier
  review/propose slice can outlive its lease while genuinely running — bare
  `lease < now()` (the ssh_node steal predicate) would false-positive and
  double-drive. Reap terminalizes to `cancelled` (tag `reaped:reboot-orphan`),
  never `failed`, so it recovers only *reboot* orphans. **Failed-rest backoff +
  surfacing (RC1, ADR 0065):** the four coordinator rest-reasons are now fully
  distinguished by the loop's terminal STATUS — `cancelled` (reboot reap) and
  `succeeded` (dry/punt/RC2) re-mint immediately, but a loop that rested
  `STATUS:failed` (the `_max_tick_failures` budget — ≥5 consecutive hard
  failures = a persistent break, or a crashed slice) is held out of the re-mint
  by `_failed_rest_cooldown_active`: an **escalating cooldown**
  `min(BASE·2^(n-1), CEIL)` (`PRECIS_QUEST_LOOP_FAIL_BACKOFF_S`/`_MAX_S`, 30 min
  → 6 h), `n` = the trailing run of consecutive failed rests read from job
  history (no stamped counter). A permanently-broken quest thus retries at a
  30 min → 6 h cadence instead of every worker pass — self-healing a transient
  outage, throttling a real break; `reconcile_quest_loops` tallies it as
  `backoff`. The nursery surfaces it: a `quest-loop-failing` detector (`warn`,
  > `QUEST_LOOP_FAIL_24H`=3 failed loops/24 h, mirrors `plan-tick-spin`) raises a
  `/alerts` warning so a human fixes the config or pauses the quest rather than
  letting it burn compute invisibly. **Division of labor:** the reconciler owns
  quest-coordinator orphans (quest context + every-pass cadence); the sweeper
  stays the general coordinator/`claude_inproc` backstop (and covers quest loops
  if the reconciler is gated off). Both terminalize under `FOR UPDATE`, so a
  rare double-fire just leaves the job terminal either way. **Anti-stall
  commit ladder (gripe 171149):** `quest/tick.py::run_quest_tick` tracks
  `meta.ticks_since_experiment`; once a tick's compute phase dispatches 0
  sims for `PRECIS_QUEST_FORCE_EXPERIMENT_EVERY` (default 2) consecutive
  ticks, `_commit_reprompt_ladder` re-prompts the same model in propose-mode
  with a hard "commit now" directive (`_build_commit_prompt`) plus the
  tried-set (`quest/explore.py::tried_set_summary` — a pure DB-fact read of
  tried candidates + measures, no chemistry enumeration); still nothing
  escalates one tier to `Tier.FRONTIER` and re-prompts once more; still
  nothing backs off honestly (a logged `decision` entry — code never
  fabricates a dispatch or picks chemistry). Every tick prompt also carries
  the **explorer's creed** (`_explorers_creed`): a *moving* champion-to-beat
  (the frontier-best, never a fixed threshold), mechanism→variant reasoning,
  and a ban on the model ever declaring the quest "solved"/"done"/"closed".
  Design principle: **the discovery agent owns all chemistry** (element,
  site, coverage, co-adsorbate) — `catalyst_seed.py::PARAM_SPACE` is now
  coverage-count + the fcc111-buildable fact only, not a chemistry menu;
  graduation (`graduate.py`) stays a per-candidate milestone (a
  `needs-experiment` deed) and never halts the search. **Web-surfaced**:
  `/refs/quest/<id>` is a dedicated hub dashboard (`precis_web/routes/refs.py`'s
  `detail()`, `refs/quest_detail.html.j2`), not the generic ref-detail render —
  header (status/prio/momentum/tote), hub links (dossier, paper draft when a
  `paper-of` edge exists, log/frontier/gaps), a "happening now" recent-log
  callout, dossier narrative+ledger, logbook tail, frontier/gaps panels, and a
  servers-lite kind-count footer replacing the old raw-handle-link dump.
- **`llm`** — the model catalog (design-of-record `docs/proposals/llm-catalog.md`;
  slice 1 **live, read-only, ships dark**). Turns model choice from hardcoded
  constants (`router._TIER_MODEL` + `meta.llm_tier=opus|sonnet|haiku|local`) into a
  first-class kind: a **catalog** of facts + a **ledger** of observations + a
  **policy** that picks (quest/gripe shape — numeric-ref `handlers/llm.py`, handle
  `lm`, migration 0071, `emits_card` so a card *is a vector*, `corpus_role='none'`).
  Identity = one ref per model; `meta` carries `model_id` (the human key
  `get(kind='llm', id='claude-opus-4-8')` resolves via `store.find_ref_by_meta`),
  `tier_floor`, `offerings` (operating points — effort/window are axes *within* a
  card), coarse 1–5 `capability` axes, and provenance. The shared writer
  `precis.llm_catalog.upsert_card` (idempotent on `model_id`) is used by both the
  handler and the reconcile pass. **`llm_reconcile`** (`workers/llm_reconcile.py`,
  the `paper_reconcile` cadence + xact-lock shape, **default-OFF**
  `PRECIS_LLM_RECONCILE_ENABLED` / `--only llm_reconcile`) refreshes facts from the
  live OpenRouter feed (`/api/v1/models` — no key) + flags **proxy drift** (a card
  whose loopback-proxy offering names a model the proxy can't serve — the
  opus-not-in-proxy 400) via `raise_alert`. Seed + drive: `precis llm
  seed|reconcile|list`. **`seed_frontier_cards`** (`precis llm seed --frontier` /
  `--all`) additionally mints a curated **frontier open-weight ladder** (Opus→Haiku,
  all tool+reasoning-capable: GLM-5.2 / Kimi K3 / DeepSeek-V4 / Kimi-K2.7-Code /
  Qwen3.7-Max / MiniMax-M3 / GLM-4.7 / Qwen3.6-Flash / DeepSeek-V4-Flash /
  GLM-4.7-Flash / gpt-oss-120b/20b) with `openai_compat` offerings (window+price
  from the live OpenRouter snapshot) + provisional `published-benchmark` capability
  ordinals — the open-weight menu `select_offering` picks from; `record_observed_axes`
  / `record_eval` overwrite the ordinals with higher-trust numbers once they run.
  Empty catalog ⇒ byte-identical to today (`Tier` stays the
  floor). **Variant-precise offerings (gripe 162624) built**: one OpenRouter slug
  fans out to ~28 bookable **endpoints** differing by provider / quant (fp4≠fp8) /
  window (1M..101k) / price, so capability + price are *endpoint*-scoped, not
  card-scoped. `llm_reconcile` now also pulls `/models/{slug}/endpoints` → a
  machine-maintained `meta.endpoints` list (kept separate from the curated
  `meta.offerings` — reconcile never clobbers a seed) + nightly per-variant prices;
  `record_benchmark` stamps a `published-benchmark` ordinal onto *only* the matching
  quant's endpoints (a fp8 SWE-bench number the fp4 variant can't inherit); frontier
  cards seed `meta.params` (size/arch/license — OpenRouter carries no param count).
  `admit.window_for` / `policy._model_price` read the widest/cheapest endpoint;
  `select_offering` returns `Selection.endpoint` (cheapest fitting variant), which a
  caller threads onto `LlmRequest.endpoint` so `_dispatch_openai_compat` emits the
  OpenRouter `provider:{order,quantizations,allow_fallbacks:false,require_parameters:
  true}` + `reasoning:{effort}` pin (`router.openrouter_routing`) — reproducibly
  hitting that provider×quant instead of load-balancing the 28. Ships dark: no
  endpoints ⇒ the bare slug is posted (today's behaviour); the pin only engages
  under `PRECIS_LLM_BACKEND=openai`. **Slice 2 (`admit()`) built** (`utils/llm/admit.py`): a pure
  `est_tokens×(1+headroom) ≤ max_input` fit-check wired into `router.dispatch`
  after `gate_tier` — refuses a doomed (context, model) pairing *with the numbers*
  as a normalized `LlmResult.error` (never raised, so a pinned pass backs off, not
  spins); a deduped `raise_oversize_alert` (source `admit:oversize`) is the
  pass-level page. Window comes from the offering's `max_input` else the
  reconciled `facts_openrouter.context_length`; a short-TTL in-process catalog
  cache keeps the hot path a dict lookup. Ships dark: no store / no card / no
  known window ⇒ admit is a no-op. Standalone `admit_context` is exported for the
  context-assembly path (split/trim before forming a request). Still-direct
  `claude -p` passes (`plan_tick`/`fix_gripe`) bypass dispatch → not yet gated
  (deferred). **Slice 3 (ledger) built** (`llm_catalog.py`): the WORM **review
  log** (`llm_review` chunks, the quest-logbook pattern — typed evidence bands
  `published-benchmark`/`measured-eval`/`observed-telemetry`/`agent-review` with
  provenance, appended via `put(kind='llm', id=N, text=…, entry=…, by=…)`, read
  via `view='reviews'`); the **tote** (`llm_tote` rollup over `llm_call_log` per
  model — calls/cost/error-rate/p50/turns, read via `view='tote'`, a live query
  not stored); **observed-axis derivation** (`record_observed_axes` — the
  operational reasoning signal: success rate → a 1–5 `reasoning-convergence`
  ordinal with `observed-telemetry` provenance); the `measured-eval` write surface
  (`record_eval`); and `PROVENANCE_TRUST` (observed>measured>published, so bands
  never blend). CLI: `precis llm tote|observe`. **Deferred:** the full golden-
  task-from-corpus eval harness (real fix_gripe/needle/summarize runs → ordinals;
  `record_eval` is the write surface it reports through). **Slice 4 (policy)
  built** (`utils/llm/policy.py`): `select_offering(store, Requirement) → Selection`
  — deterministic requirement→model, a **decision-point** call (not the hot path;
  ranking never runs per-item). Hard-filter (window via `admit`, budget band via
  `gate_tier`, availability, `supported_parameters` flags) → rank (survivors ≥ the
  dominant axis's min ordinal, cheapest wins) → a Pareto **`next_better`** rung
  over (capability↑, cost↓) reusing `quest.frontier.pareto_split`. The invariant:
  empty catalog / nothing-fits ⇒ `resolve_model(tier_floor)`, **byte-identical to
  today** — so a call site can route through it before any card exists. The LLM
  infers the `Requirement` (slice 5); the policy stays deterministic + price-aware.
  CLI: `precis llm select`. **Deferred:** wiring the deliberative call sites +
  `plan_tick` through the catalog + the transport-on-card collapse
  (`LITELLM`+`OPENAI_COMPAT` → one param'd provider) — progressive integration, not
  the policy core. **Slice 5 (agent surface) built** (`utils/llm/requirement.py`):
  the **task→requirement judge** — `infer_requirement(task) → Requirement` runs a
  cheap (`MEDIUM`) one-shot judge that infers a *capability requirement*
  (never a model name — the LLM is price/window-blind + self-biased), and
  `choose_model(store, task)` chains it into `select_offering`. Every field is
  clamped so a malformed reply can't produce an illegal requirement; the judge is
  injectable for tests. Plus the agent-facing `precis-llm-help` skill (express a
  requirement, don't pick a model) and CLI `precis llm choose`. **All 5 slices
  built + green** (facts → guardrail → ledger → policy → agent surface); ship dark.
- **`alert`** — machine-detected ops/health conditions (spin loops, orphans),
  raised via `precis.alerts.raise_alert` (fingerprint upsert + auto-resolve),
  read via `AlertHandler`/`/alerts`; manual resolve flips the state tag +
  `resolved_at` column together everywhere — the tab's per-row dismiss button
  posts to `precis.alerts.resolve_alert`, and the agent `tag` verb syncs the
  column via `AlertHandler._after_tag_mutation` →
  `sync_resolved_at_with_tags` (reopen-by-tag clears it; rejected if the
  condition re-raised as a fresh open row). **Not embedded.**
  Skill: `precis-alert-help`.
- **`agentlog`** — per-run attribution record (prompt + model + `touched` links
  to every chunk a run wrote), **not embedded**; `precis.agentlog` write side,
  sweeper GCs past `PRECIS_AGENTLOG_RETENTION_DAYS`. The `touch_from_env` hook now
  fires on cache-backed (websearch/web/perplexity) + memory writes too, not just
  draft; `dream_agent` ticks open one. Skill: `precis-agentlog-help`.
- **`job` substrate** — `meta.job_type`+`meta.executor`, `STATUS:` tag,
  forensics as `job_event`/`job_summary`/`job_result` chunks; `claude_inproc`
  executor; `fix_gripe` is the reference job_type. The `claude_docker`
  executor (`job_claude_docker` pass, **default-OFF** under
  `PRECIS_SANDBOX_ENABLED`) runs the `sandbox_run` job_type as a detached,
  cgroup-capped, poll-reaped container on an `agent_sandbox_host` — slice 1
  is the stub-podman substrate (mint→claim→launch→poll→terminal, `mode:build`
  only). **Slice 2 (harvest, built)**:
  `workers/executors/_sandbox_harvest.py` — on a clean `mode:build` exit,
  `claude_docker._terminate` mints a `kind='folder'` (`derived-from` the
  job, `supersedes`-chained to the owning todo's prior build) with each
  `/work/out` file projected as a disk-backed `plaintext` ref under
  `PRECIS_ROOT/sandbox/<container>/…` (drives
  `PlaintextHandler._ensure_ingested`, mirroring `precis.sim.ingest`; skips
  gracefully — tarball-only — when `PRECIS_ROOT` is unset), tars `out/`
  content-addressed (`PRECIS_SANDBOX_ARTIFACT_ROOT`, sha256-keyed;
  gzip via stdlib `tarfile` for now — the design's `.tar.zst` naming is a
  later, non-breaking codec swap) and parses `RUN.json` onto the
  folder's/job's `meta` as the `mode:run` recipe. `job_summary` taxonomy
  (success w/ files, empty `out/`, non-zero exit, timeout) unchanged on the
  todo's pass/fail bit — the taxonomy is forensic text. **Slice 3
  (`mode:run`, built)**: `params.artifact` (a prior build's harvested
  `folder` ref id — `semantic_rejection` requires it, no `prompt`) is
  staged into `/work` by `_sandbox_harvest.stage_run_artifact` (tarball,
  sha-verified; on a miss, reconstructs from the folder's `plaintext`
  refs via each ref's stamped `meta.harvest_orig_path`, undoing the
  legible-projection filename rewrite); `claude_docker._launch_run` +
  `build_rerun_argv` launch `sh -c 'cd /work && uv sync &&
  <RUN.json.cmd>'` with **no** claude/OAuth/API-key env at all (no
  backend-flip gate either — no claude CLI is spawned); the result is
  harvested through the same `harvest_out` as a build, plus a
  `derived-from` link from the run's folder back to the build folder
  (the design's "`run-of` link" reuses `derived-from` rather than a new
  relation/migration; `meta.run_of_folder_id` disambiguates the two
  `derived-from` edges). A `mode:run` recurring needs no new scheduler
  code — `meta.schedule`'s existing spawner already carries
  `executor`/`job_type`/`params` onto each spawned tick.
  **Slice 4 (`precis_access:read`, built)**: `precis serve` gains an
  optional `--transport sse|streamable-http --host --port --token`
  (`stdio` stays the byte-identical default — `main()` still calls
  `mcp.run(transport="stdio")` unchanged; a network transport builds its
  own `uvicorn.Server` around `mcp.streamable_http_app()`/`sse_app()`
  wrapped with a bearer-token `BaseHTTPMiddleware`, since `FastMCP.run()`
  gives no middleware hook). `claude_docker._launch_build` (never
  `_launch_run` — `mode:run` gets no `mcp.json` ever) spawns a per-run
  `python -m precis serve --transport streamable-http` child via
  `_sandbox_read_mcp.spawn_read_mcp` when `params.precis_access ==
  "read"`: `agent_ro`-DSN'd (`read_only_database_url` swaps `user` on the
  daemon's own base DSN and strips the password — the host's
  `~/.pgpass`, already cluster-provisioned by `deploy/roles/pgpass`,
  resolves it), bound to `127.0.0.1:<ephemeral port>`, a fresh per-run
  token. `/work/mcp.json` points the container at it; the container's
  `--network` swaps to `slirp4netns:allow_host_loopback=true`
  (`_sandbox_read_mcp.READ_MCP_NETWORK`, container-side host `10.0.2.2`)
  ONLY for that launch. Only the PID persists to `job.meta.read_mcp_pid`
  — never the token/port, which would otherwise leak a live endpoint's
  credential into a broadly-DB-readable job ref. Teardown
  (`reap_read_mcp`: SIGTERM→grace→SIGKILL by PID, mirroring
  `cli/watch.py::reap_tracked_process_groups`) is wired into every
  terminal path: `_terminate` (covers both a normal exit and a deadline
  kill — both route through it) and `reconcile_orphans` (a worker-crash
  recovery window). Gated fail-closed by `PRECIS_SANDBOX_READ_MCP` in
  `sandbox_run.semantic_rejection`. **Per-unit pinned image provenance
  (built, small)**: the launched `image` (`params.image` override or
  `code-task:<git-sha>`/`latest` default) is recorded in three places —
  `job.meta.image`, the terminal `job_summary` chunk text
  (`image=<image>.`), and the harvest folder's `meta.image` — for both
  `mode:build` and `mode:run` alike. See `docs/design/sandbox-run.md`.
  Skill: `precis-job-help`. **fix_gripe
  routes through the `call_claude_agent`
  chokepoint (§H cycle a)** — no more bare `subprocess.run` of claude: an
  isolated env (`env_base=_restricted_env(...)`, no DB creds, `--bare`
  API-key auth), an explicit fix_gripe envelope (`egress:api-only`), and a
  bind mount (`agent_container.Mount`, `-v`/`-w` support added this cycle —
  also unblocks gr178973) for the clone ONLY — never the source repo. The
  agent commits inside the clone; it has no filesystem path or network
  route to origin, so it cannot push. **Write-back is a commit, pushed on
  the trusted side**: once `call_claude_agent` returns, `run()` (host-side,
  never inside the sandbox) performs the `git push` itself, guarded
  host-side to `gripe_<id>` branch names (alongside the clone's own
  pre-push hook — belt and braces, not the only defense). A
  **containerized** run (whenever `PRECIS_AGENT_CONTAINER` is on and the
  host can run it) is network-isolated and needs no operator ack; when the
  container path is unavailable (feature off, probe-failed, or an infra
  failure mid-run) the chokepoint's `require_container=not
  _unsandboxed_ack()` refuses to fall back to running unsandboxed
  (`ContainerRequiredError`) unless `PRECIS_FIX_GRIPE_UNSANDBOXED_ACK=1` —
  so enabling `backlog_groom` alone still can't unleash an unsandboxed run
  (gr179498, fail-closed retained for the no-container case). The
  `precis-agent` image (`docker/Dockerfile`'s `agent` stage) now also
  carries git + uv + a minimal build toolchain so the containerized agent
  can clone/edit/test.
- **`structure`** — atomistic cell+bond IR (ADR 0043); typed ops + in-memory
  probes, relax on the GPU node (derived-lane job, ADR 0044), cursors/measures
  on `struct_measures`, web `/structure` (3Dmol viewer: zoom + look-inside
  near-clip sliders; generic `/refs/structure/{id}` 303-redirects here).
  `slab` op hardened against messy LLM
  JSON (null/list params → clean `OpError`, not a crash); `invariants.py` gives a
  representation-invariant fingerprint (composition · per-layer · adsorbate site ·
  coordination) powering the **round-trip eval** (`scripts/llm_eval/roundtrip.py`,
  `docs/design/structure-roundtrip-eval.md`). `structure_propose` build step
  pinned to BIG=sonnet (ties opus at ½ cost; reasoning stays on FRONTIER). Skill:
  `precis-structure-help`.
  - **Pre-dispatch pre-flight gate (gr51393).** A relax that would dispatch to
    the GPU (`handlers/structure.py`, the `RelaxUnsupported` branch) first runs
    `validate()` as a **hard-reject** — overlap / over-valence / impossibly-long
    declared bond (`validate.py::BOND_LENGTH_FACTOR`=1.3× covalent sum, `declared`
    bonds only) raises `BadInput` naming the atom pair and mints **no** job — then
    a local `clean` (default) or opt-in `preflight='emt'` pre-relax, re-checking
    the cleaned geometry's `cache_key` for a completed run before dispatch. Cloud
    is last-resort; a plain local `clean`/`emt` edit is never gated.
  - **Tier-0 substrate preflight (`structure/preflight.py`, gr172323, flag
    `PRECIS_STRUCTURE_PREFLIGHT`, default OFF → dark).** A fast, synchronous,
    element-agnostic sanity gate for catalyst candidates, *in front of* any MLIP
    compute. `preflight(scene) → PreflightVerdict(ok, reasons)` runs: (1) an
    ASE-free **element-in-box** check (element ∈ the deployed MLIP's set —
    `MACE_MP_ELEMENTS` ≈ 89, not EMT's 7; outside → reject) then (2) a **dumb
    universal relax** (`_DumbField` — covalent-radius springs + soft repulsion,
    any element, ~30 FIRE steps) and (3) post-settle judgments — clash / detached
    (fly-off) / ceiling (into the vacuum) / no-vacuum / porous / internal-void —
    each a **per-atom actionable reason** (names the atom handle + a fix verb).
    Wired two seams: `handlers/structure.py` `put`/`edit` **reject + undo** the
    edit before the version commits (fail-open on missing ASE/[dft]); and
    `quest/compute.py::dispatch_autocatpath` **mints no job** for a failing substrate
    and stamps a `ruled-out:preflight` dead-end the proposer reads. Catches
    *authoring* faults (badly-placed / spongiform / out-of-box); *physical*
    desorption of a well-authored slab stays the autocatpath-tier (MLIP) verdict.
    Slab/adsorbate split falls back to a dominant-element heuristic (Scene has no
    slab provenance yet — OPEN-ITEMS follow-up).
  - **Structure ↔ literature loop (gr161578/gr161577).** `view='literature'`
    assembles a **deterministic** paper query from the design (description +
    host-metal(s)/adsorbate/facet from `scene.composition()`, all host metals
    kept for alloys; formula only as last-resort) and runs the shared paper
    search (`PaperHandler.search_hits`, embed→lexical degrade). The TOC + web
    `structure_detail` surface **paper-provenance** links (any paper-target link
    + DOI + a rationale note) so a design shows *why* it was made;
    `link(..., note=…)` stores the rationale on the edge (`links.meta`, opt-in
    `add_link(merge_meta=True)` — no other link caller's conflict behavior
    changes). `motivated-by` relation is a follow-up (only `cites`/`related-to`
    seeded today; the surface is relation-agnostic).
  - **Run-scoped per-atom forces (gr161576, mig `0087`).** `struct_runs` gained
    nullable `forces`/`charges` jsonb. Local EMT/ML relaxes now keep their
    per-atom force array (was reduced to scalar `max_force` and dropped); the
    `clean` rung / on-demand path gets a cheap ASE-EMT single-point estimate
    (`relax.estimate_forces_emt`, closed EMT set only, `approx=true`, never
    fabricated). Stored **label-paired** (`{"vectors","labels","approx",
    "source"}`, `cache.serialize_forces`) — NOT canonical-rank-indexed, because
    rank sorts on position and a periodic-wrap relax reorders it → the read join
    is a stable-label lookup, never a re-derived rank (would mis-attribute
    forces on same-element slabs). `view='atom'` shows `|F|` + vector +
    approx/fidelity label (`run=<id>` pins a run; unpinned = current-version run
    only, else the estimate); TOC/`view='runs'` show forces from a recorded run
    only (no live EMT on the default read). `charges` = reserved null slot (no
    backend produces partial charges). Cloud `struct_relax` writeback leaves
    both null. Purpose is a qualitative "which atoms are strained" sense for the
    modeling LLM, not physics-grade truth.
  - **External DFT catalyst import (ADR 0053, Sequencing steps 0–2 shipped).**
    A new fidelity rung `emt` (`structure/relax.py::_relax_emt`) — ASE-EMT +
    FIRE, torch-free, gated behind the light `[dft]` extra (never `[dft-ml]`,
    never dispatches to the GPU node), element-guarded to the closed set
    `{Al,Ni,Cu,Pd,Ag,Pt,Au,H,C,N,O}` (`RelaxUnsupported` outside it). The
    run-cube (`struct_runs`) gained a `provenance` (`computed`/`external`) +
    `method` JSONB axis (migration `0084_struct_runs_method_provenance.sql`);
    the cache-hit partial index is narrowed to `provenance='computed'` so an
    imported row can never serve a compute cache hit. A pure adapter registry
    (`structure/importers/__init__.py::register_adapter`/`get_adapter`, IR:
    `ExternalId`/`ExternalRun`/`Adapter`) feeds the one idempotent write path
    `store/_structure_ops.py::StructureMixin.structure_import` — keyed on
    `ref_identifiers (dataset, config_id)`, a re-import updates in place, never
    duplicates. `handlers/structure.py::StructureHandler.edit` refuses on a
    `provenance:external` design ("derive a variant instead");
    `guard_energy_comparable` refuses a cross-method-fingerprint ΔE. First
    adapter: Catalysis-Hub (`structure/importers/catalysis_hub.py::adapter` +
    `fetch_config`, SSRF-guarded GraphQL, `[import]` extra) wired to on-demand
    hydrate — `handlers/structure.py::StructureHandler._get_external` via
    `get(kind='structure', args={'source':'catalysis-hub', ...})`; a
    `config_id=` hit short-circuits to a network-free cache read, a broad
    filter fetches + renders a summary table. **Live access (verified
    2026-07-24):** Catalysis-Hub locked down *all* public programmatic access —
    the GraphQL API 401s without an `X-API-Key`, and the "public" `apiuser`
    Postgres password in `cathub/config.py` was rotated server-side (host
    reachable, auth fails). So the on-demand REST path is code-complete but
    **dark** pending a SUNCAT credential. **Batch-mirror ingress (shipped,
    keyless):** `structure/importers/cathub_db.py::read_cathub_db` +
    `batch_import` mine a local cathub `.db` (self-contained SQLite: relational
    `reaction` table + embedded ASE `systems` + `publication`) through the
    *same* `catalysis-hub` adapter — importing each reaction's product adsorbate
    system (skips clean-slab/gas refs), idempotent on `(dataset, config_id)`;
    needs only ASE (`[import]`), no `cathub` dep and no network. Remaining
    (Sequencing 3–6): the `precis import <source> --filter` CLI + resumable
    cursor; a first *open* bulk-source adapter (OC20 anonymous / AQCat25
    HF-login — both verified keyless-reachable 2026-07-24) so the mirror has a
    live corpus; promoting `source=` to a first-class top-level `get` param
    (today via `args={'source':...}`); MLIP fine-tuning on the imported corpus.
- **`citation`** — verifier-workflow kind (`text`+`source_handle`+`source_quote`
  +`verifier_confidence`, `link='paper:<slug>'`); tex `\citequote` persists the
  same quote. Skill: `precis-citation-help`.
- **`cfp`** — spec-role sibling of `paper` (proposal requirements doc); same
  Marker→chunks ingest + reader, `KindSpec.corpus_role='spec'` (never cited as
  evidence), links to its project via `has-requirement`. Skill: `precis-proposal-help`.
- **Term registry (`draft`, ADR 0052)** — glossary / patent parts / manufacturing
  components are one abstraction over the `chunk_kind='term'` leaf, discriminated by
  `meta.registry ∈ {glossary,parts,components}` + a per-registry numbering policy
  (`src/precis/draft/registry.py`: `components→insert`/frozen `meta.callout`,
  `parts→render`/positional numerals). Store: `defined_terms` (rich hover map) +
  `ensure_registry_heading(role)` (lookup-by-tag → adopt-legacy → one-per-role
  reconcile) + `parts_callout_map`. Reader: rich `.pa-pop` card (MPN/mfr/datasheet)
  + a bare `[[dc…]]` part ref renders as its numeral (`linkify.callouts`). No new
  kind, no migration. Section-style skill: `components.md` (+ `patent-image-part`).
- **Keystone kinds (`cad`/`pcb`/`structure`)** — "own a legible IR, rent the
  heavy kernel only at export" (ADR 0041/0042/0043); the LLM traverses a graph,
  never pixels. `pcb` exporters in `src/precis/pcb/export.py` (JLCPCB BOM/CPL —
  **footgun:** CPL wants CCW, `jlc_rotation(r)=(360-r)%360`), route via
  `pcb/route.py` (headless Freerouting, skips if absent). Skills: `precis-pcb-help`.
- **`cad` web editor (`/cad`)** — three.js viewer + edit-by-prompt. Viewer
  tessellates **client-side** from a ~1 KB recipe (`GET /cad/<slug>/scene.json`)
  via `static/cad-tessellate.js` (a port of `cad/tessellate.py`, drift-guarded
  byte-for-byte by `tests/test_cad_parity.py`, node-gated); `model.gltf` kept for
  download + solid-mode. Server-side STEP/STL/3mf/scad export; `cad_propose` job →
  `CadHandler.derive`. Analysis is off the render path (`GET /cad/<slug>/analysis`,
  memoised); `cad/bulk.py` volume is an exact ray-interval quadrature, not the old
  200k-point Monte-Carlo. Drive (`/drive`) is the default landing. Skill: `precis-cad-help`.
- **Broad + deep paper search** — Tier 1 `search(kind='paper', queries=[…],
  answers=[…HyDE], per_paper=N)` RRF fusion; Tier 2 `good=True` mints an async
  `good_search` coordinator campaign. `docs/design/good-search-coordinator.md`;
  skill: `precis-search-help`.
- **`chunks.numerics TEXT[]`** — GIN-indexed lexical filter
  (`WHERE numerics @> ARRAY['1.523 eV']`); direct-SQL only, not yet in search verbs.
- **`precis web`** — browser UI (Tasks/Papers/Console/Conversations/Status).
  Two-pane paper reader (`routes/papers.py` + vendored pdf.js); the **sole draft
  reader is `/smartdraft`** (`routes/smartdraft.py` + `smartdraft.py`) — the
  three-pane fisheye reader (left TOC · middle focus+neighbourhood · right
  collaborate). The classic virtual-scroll reader (`GET /drafts/{ident}`) was
  **retired**: that path (and `/draft/{ident}`, and every `¶`/`§` `/c/{handle}`
  deep link, `refs._quest_draft_url`, agentlog back-links) now 307/303-redirects
  into smartdraft, focused by `?focus=<dc|base58>` (`focus_index` accepts both).
  `routes/drafts.py` **remains** as the shared backend + library smartdraft
  reuses (~20 endpoints: all editing/export/figure/lifecycle, plus the ported
  `/human-review` + `/review`); only the reader page + its reader-only endpoints
  (`/skeleton`,`/rows`,`/row`,`/version`,`/wordcount`,`/find`,`/around`,
  `/listkind`,`/style`,`/prompt`,`/todo/*`) and templates (`detail`/`_row*`/
  `prompt_preview`) were removed. Smartdraft's **fisheye** mode is budget-bounded
  (`_TOC_BUDGET`); its **full-document** mode (📄, `relevance=0`, **now the
  default**) renders a window around the focus and lazily hydrates distant
  chunks on scroll (IntersectionObserver → `GET /smartdraft/{ident}/blocks`,
  `_FULLDOC_WINDOW`), so a 10k-chunk draft loads O(window), not O(N).
  **Keyboard** (`view.html.j2`, all client-side): `/` search · `p` pin ·
  arrows/Tab outline-walk · `i`/`Enter` edit the focus block (Esc leaves, via
  ProseMirror) · **`R`** starts an in-place Spritz/RSVP reader — walks words
  from the focus onward at a WPM rate, decorating each word in place (green box +
  red ORP pivot) and auto-scrolling, growing its stream as full-doc placeholders
  hydrate ahead (R pause/resume · Esc exit · +/- speed · ↑↓ ¶ · ←→ sentence ·
  Space next ¶). `precis_web` is a sibling package over the handlers (ADR 0026).
  **Export can bundle the cited sources** (`export/sources.py`,
  `collect_cited_sources`/`build_sources_zip`): the reader's `+ sources`
  checkbox appends every cited paper/datasheet PDF the host holds to the PDF as
  a `pdfpages` appendix (`export_draft(include_sources=True)`) — Word gets a zip
  (`report.docx` + `sources/`) since it can't embed PDF pages — and
  `GET /drafts/{id}/papers.zip` (also `precis draft papers`) zips just the cited
  PDFs + a `manifest.txt`. PDFs resolve via the same corpus resolver as
  `corpus_reconcile` (`corpus_layout.rebase_onto_local`); the corpus being
  per-host, unlocatable sources are listed in the manifest rather than failing.
  **Review-status surface** (`docs/proposals/smartdraft-review-status-ui.md`,
  full — supersedes the classic reader's retired F/C/S/A strip). Ledger:
  `chunk_review(chunk_id, checker, approved_sha, verdict, at)` (mig 0086),
  a per-(chunk, checker) watermark — "dirty" is derived
  (`approved_sha IS DISTINCT FROM chunks.content_sha`), never a loop
  (`src/precis/store/_draft_ops.py::DraftMixin.review_status_for_draft` /
  `DraftMixin.chunks_requiring_review`). One special case: the
  document-altitude `toc` checker rides the draft's first-in-order chunk
  and pins to `DraftMixin.toc_digest` (a sha256 over ordered `(heading
  chunk_id, content_sha)`) instead of that chunk's own sha — a heading
  add/remove/rename/reorder dirties it, a paragraph body edit doesn't.
  **Checker namespace** — `flow`/`cites` (sonnet, per-weave/local),
  `structure`/`adversarial` (opus, mint on HEADING chunks only — the
  anchored reviewer already renders the whole section via fisheye) and
  `toc` (opus, one review-todo per document); `human` is the fixed point
  that supersedes every machine lens (`src/precis/quest/review_fanout.py`
  `ALL_LENSES`/`DOC_LENSES`/`_LENS_TIER`). **Incremental fanout** —
  `src/precis/quest/review_fanout.py::mint_review_fanout(only_dirty=,
  scope=)` mints only `(chunk × lens)` pairs not yet approved at the
  chunk's current sha/digest; `scope=` narrows to a heading's subtree or
  one chunk (`DraftMixin.review_subtree_chunk_ids`); a chunk carrying an
  open anchored change-request is skipped (`unsettled_skipped`) via the
  shared guard `src/precis/quest/review_guard.py::
  has_open_change_request`/`has_open_change_request_via_store` (also
  used by the writeback, so mint-time and record-time agree). Lens ×
  chunk-kind mapping keys off `PROSE_CHUNK_KINDS`
  (`src/precis/utils/wordcount.py`) — `flow`/`cites` mint on prose
  chunks only, never headings/equations/tables/terms. **Writeback** —
  `src/precis/workers/executors/claude_inproc.py::
  _maybe_record_review_pass`, gated on a clean non-resumed `verdict:
  done` tick with zero filed findings and no open change-request; for
  `toc` it recomputes `toc_digest` and compares against the digest
  captured at tick start (`_anchor_chunk_snapshot`) rather than a chunk
  sha. **Web** — `src/precis_web/routes/drafts.py`'s `POST
  /drafts/{id}/review` (`review_block`) now drives `mint_review_fanout`
  under the unified lens vocabulary (`flow` | `cites` | `structure` |
  `adversarial` | `toc` | `all`), keeping the old `structural`/
  `deep_review` names only as aliases onto `structure`/`adversarial`
  (`_LENS_ALIASES`); `POST /drafts/{id}/review/retract`
  (`retract_review_route`) un-reviews any checker
  (`DraftMixin.retract_review`); `POST /drafts/{id}/cites/convert`
  (`convert_cites_route`) wraps `src/precis/taproot/backfill.py`'s
  `plan_chunk`/`apply_chunk` (dry-run preview, then apply through the
  normal edit door so approvals go stale by construction). **Smartdraft
  UI** — every rendered block (not just focus) gets a 4-state indicator
  (`src/precis_web/smartdraft.py::review_indicator`: empty/grey →
  machine/hollow-blue (own lenses + enclosing-heading's `structure`/
  `adversarial` "via section") → human/green → dirty/amber) with a
  per-checker tooltip matrix (`_matrix_row`) and a deterministic (never
  sha-pinned) citation-integrity flag (`cite_integrity_ok`, computed at
  read time — a cited paper can vanish from the corpus without the
  paragraph's text changing). Indicator dropdown
  (`src/precis_web/templates/smartdraft/_block.html.j2`'s
  `sd_review_widget` macro): mark/un-review, run one lens or all (a
  heading's "all" covers its subtree), convert to living cites,
  diff-since-approval. Toolbar badge (`sd_review_badge` macro, same
  template) shows prose-only `N/M done`
  (`DraftMixin.review_rollup_for_draft`) with a dropdown: per-checker
  counts (`src/precis_web/smartdraft.py::checker_rollup`), the taproot
  hub-coverage scoreboard + citation-lifecycle counts (reused from
  `_hygiene_lines`/`_citations_view.py`), and the deterministic
  document-shape half — per-section word-count balance
  (`aggregate_word_counts`); scaffold-completeness is NOT computed
  (`DraftMixin.scaffold_sections` persists no expected-section list to
  diff against — the dropdown surfaces that absence via `scaffold_note`
  rather than inventing a scaffold store). `GET /smartdraft/{id}/blocks`
  hydration carries the same per-chunk `review_by_dc` payload
  (`src/precis_web/smartdraft.py::review_payloads_for`) as the initial
  render.
- **SSRF guard** — `src/precis/utils/safe_fetch.py`; classifies every host
  against the private/loopback/link-local/cloud-metadata blocklist.
  **Connect-layer pinning** (`pinning_transport` → a custom httpcore
  `SyncBackend`, gr179502 + gr180122): `_classify_and_pin_host` runs inside
  `connect_tcp`, so the host is resolved **once** and that exact validated IP
  is dialed (no DNS-rebinding TOCTOU) while the request URL keeps its
  **hostname** — so httpcore's pool key + TLS `server_hostname` stay
  per-hostname (no cross-host connection reuse; gr180122 closed the pool-key
  collapse the earlier URL-rewrite design caused). `http_client()` installs the
  transport by default; every `safe_get`/`safe_stream` caller routes through it
  and the helpers **fail closed** (`_assert_pinned_client`) on an unguarded
  client. `resolve_pinned_ip`/`assert_public_http_url` remain standalone
  pre-check helpers (not on the send path). Direct tests in
  `tests/test_safe_fetch.py`.
- **LaTeX compile hardening** — `src/precis/utils/tex_hardening.py`; both compile
  paths (`compile_guard`, `export/compile`) run over agent-authored workspaces.
  `hardened_latex_env` pins `shell_escape=f` (closes `\write18` RCE). `latexmk_argv`
  + `trusted_latexmkrc` run `latexmk -norc -r <packaged-rc>` (gr178973) so the
  agent's workspace `.latexmkrc` (arbitrary Perl = RCE) is never read — only the
  trusted packaged rc (which still supplies `$pdf_mode=4` + the makeglossaries
  cus-dep). Deferred: `openin_any`/`openout_any=p` (needs a live lualatex+glossary
  compile check — may break the font cache).
- **Ingest hygiene** — pysbd sentence splitter in the chunker fallback chain;
  dehyphenation in `marker._clean_text`; HNSW index on `chunk_embeddings.vector`.
- **`asa-slack`** — Slack bridge sibling to `asa_bot` (`src/asa_slack/`), Socket
  Mode. Routes each turn through the ADR-0046 router (`Tier.BIG` — sonnet
  forced) via a single blocking `dispatch()` call, no live progress ticker —
  asa_bot's own Discord bridge also routes through the router now
  (router-migration Phase 3, `asa_bot/claude_invoke.py`: `Tier.FRONTIER`,
  streaming `dispatch_async` + `on_event` so the Discord progress indicator
  still ticks live), so both bridges get the budget breaker/route-log for
  free. A hard kind-allowlist (`asa_slack/kind_policy.py`, via
  `LlmRequest.env_overlay`'s `PRECIS_KINDS_DISABLED`) restricts Slack turns to
  research lookups + `memory` — `job`/`quest`/`cron`/`todo` unreachable, not
  just prompt-discouraged. Every conversation is a thread (never the channel
  root); capture is unconditional (every message asa sees, human or bot, plus
  its own replies); per-person memory reuses `asa_bot.preamble.build()`'s
  existing `user:<handle>` mechanism unchanged. ADR: `docs/decisions/0062-asa-slack-bridge.md`.
- **`fisheye`** — the degree-of-interest neighborhood render (ADR 0051 §6):
  `get(..., view='fisheye'/'fisheye+1hop')` on a chunk returns it plus its
  surroundings, not a bare chunk. Extent ladder `kwd < summary < verbatim <
  fisheye < fisheye+1hop` (`workers/working_set.py::Extent`); per-kind
  dispatch (`utils/eye_render.py::render_eye`) — tree kinds (`draft`/`plan`)
  get the reading-order spatial span (`utils/fisheye.py`, ±5 full/±10
  summary/±15 kwd, forward-biased, under the ancestor branch), doc kinds
  (`paper`/`patent`/`web`/`datasheet`/`cfp`) get the F20 keyword-cluster eye,
  link kinds (`memory`/`finding`/…) get the note + its 1-hop link ring.
  `fisheye+1hop` adds the reference ring (`utils/refeye.py`) — cited refs,
  cross-refs, linked notes, one edge out. Also surfaced by the smartdraft web
  reader (`/smartdraft/<draft>?focus=dc<id>`). Pure read-time assembly, no new
  storage. Skill: `precis-fisheye-help`.
  - **Per-chunk links panel (both readers)** — a focused `dc<id>` shows all its
    graph edges IN and OUT plus anchored change-request todos/flags: classic
    `/drafts` reader (`_row.html.j2` Col B, after Connections) and the smartdraft
    right pane (after Cited sources). One shared data path —
    `store.anchored_todos` + `precis_web/draft_links.py::chunk_links` (splits
    `store.chunk_connections`'s `direction`); the two readers differ only in
    rendering. Fixes the smartdraft gap where anchored flags rendered nowhere
    (gripe 178766). Tests: `tests/precis_web/test_drafts.py`,
    `test_smartdraft_reader.py`, `tests/test_draft_handler.py`.
  - **R1 (Claims explosion, Taproot slice)** — built. The ring gains a
    fourth group, **Claims**: a `[pub_id]` mined from the section body
    (`utils/pub_id_lookup.py`'s shared regex/lookup, factored out of
    `cli/resolve.py::_lookup_finding` so `resolve` and the ring agree on
    what a pub_id resolves to) that names a live `TAPROOT:claim` hub
    explodes into its evidence via `taproot/seniority.py::derive_evidence`
    — the claim line, its derived `establishes` originators (★-marked,
    with the grounding chunk pointer when the chase has populated one —
    `EvidenceEdge.source_handle`, read from edge `meta['source_handle']`),
    and a one-line corroborator/contradictor summary. No originator
    derived yet → falls back to corroborators "as best-available"
    (mirrors A1's `_hub_evidence_cite_keys` policy), same as `resolve`. A
    `[pub_id]` resolving to a non-hub finding (or nothing) is untouched —
    `resolve_link_targets` still doesn't mine this grammar for the
    ordinary Cited/Notes path. Rides the existing `fisheye+1hop` extent,
    no new `Extent` value. The ring now mines the hub cite in **both**
    forms — the content-hash `[pub_id]` and the finding handle `[fi<id>]`
    (the preferred surface): `_mine_claim_hub_ids` interleaves both
    grammars by text position, so first-seen order and an authorial pin on
    either form survive, and a `[fi<id>]`-shaped span already claimed by a
    `[pub_id]` match is skipped (the two grammars can collide on a pub_id
    shaped `[a-z]{2}` + 4 digits). Because a `[fi<id>]` is also an ordinary
    finding handle, `collect_ring` drops any hub ref_id present in Claims
    from Notes/Cross-refs so it renders once. Tests:
    `tests/test_refeye.py`.
  - **R2 (reader web claim rendering)** — built. The web readers render a
    cited hub (either the `[<pub_id>]` or `[fi<id>]` form) as a **violet
    claim anchor**: `precis_web/linkify.py` gains a self-contained
    `_CLAIM_CITE_PATTERN` branch (placed after the display-link pattern,
    before bare — so a display link whose label is six lowercase letters
    isn't eaten) gated on a new `claims` side-channel (the window's hub-cite
    heads, via
    `precis_web/claim_render.py::hub_cite_heads`); hover →
    `/preview/claim/<head>`, click → `/claim/<head>` (`routes/claim.py` +
    `templates/claim/`). `claim_render.render_claim_evidence` resolves a
    head (fi-handle or pub_id) to its hub and derives evidence via
    `taproot/cite.py::finding_cite_keys`, ★-marking the print-visible
    originators (corroborator fallback, same policy as A1). Both `/drafts`
    and `/smartdraft` thread `claims=` into every `linkify_refs` call and
    list cited claims in a sidebar / right-rail panel. A non-hub `[fi<id>]`
    (generic finding anchor) or bare `[pub_id]` (literal) renders unchanged
    — the side-channel defaults off, so this is a no-op outside the readers.
    Tests: `tests/precis_web/test_linkify.py`,
    `tests/precis_web/test_claim_routes.py`,
    `tests/precis_web/test_claim_reader_anchors.py`.

## Web UI (`precis_web`)

Rationalized 2026-07 (`docs/proposals/web-ui-rationalization.md`, now
shipped): the nav collapsed from ~11 top-level entries + 2 dropdowns down
to **Daily** (Drive, Tags, ToDo, always visible) · **Attention** (Needs
you / Gripes / Alerts, badged, right) · **Ops ▾** (System, Categorizers,
Agent Logs, Console, Env, Secrets) · a slimmed **Browse ▾** for the five
kind-specific readers Drive's generic rows can't reproduce (Clusters,
Structures, CAD, Figures, Mermaid). Nav template:
`src/precis_web/templates/base.html.j2`; badge counts:
`src/precis_web/nav.py::nav_badges`.

**Drive — the unified seek+manage surface (`/drive`).**
`src/precis_web/routes/drive.py::index` is Items' cross-kind chunk search
(`q=`, kind/tag facets, `sort=relevance|recency|oldest|untried`, `since/until`,
`state=stub`, pagination) grafted onto Drive's folder tree (`_flatten_tree`)
+ CRUD. `state=stub`'s downloads queue (a paper stub with a LibKey/arXiv
fetch link) defaults to `sort=untried`: `store.recent_refs(untried=True)`
`LEFT JOIN`s the latest `ref_events` row per ref with `source='manual:open'`
(index `ref_events_ref_id_source_ts_idx`) so never-manually-opened stubs
surface before re-checked ones (freshest-added first within "never",
oldest-attempt first within "tried"). The row template's "Open all
downloads" button (`drive/index.html.j2`) fires
`navigator.sendBeacon('/downloads/mark-tried', …)` alongside its staggered
tab-opening burst — one `manual:open`/`opened` `ref_events` row per shown
ref (`POST /downloads/mark-tried`, `drive.downloads_router`, un-prefixed
sibling of `drive.router`) — so those refs sink to the back of the untried
sort and a plain reload naturally serves the next batch; no pagination
bookkeeping. Same shape as the OA fetch cascade's own attempt log
(`workers/fetch_oa.py`, `source='fetcher:<leg>'`) — a human lane on the
same `ref_events` table via the shared `Store.append_event`. The pager shows First/Prev/Next/Last with a `Page X of Y` and a
`Showing N of K` count: the no-query browse total is **exact**
(`store.count_recent_refs`, sharing `recent_refs`' WHERE builder) so Last is
offered there; the fused-search total is a `≈`lexical count
(`count_blocks_lexical`) that can miss semantic-only hits, so search offers
First+Prev/Next but no Last. CRUD is
`POST /drive/new|create|{id}/rename|move|{id}/delete` + per-row
quick actions (`ItemPresenter.actions()`, `src/precis_web/item_view.py`).
The kind chips are three facet rows: **Source** (chunk-searchable corpus),
**Author** (`role='artifact'`, foldered), and **Work** (`_WORK_KINDS =
quest, todo` — agenda kinds with no body chunks, so they list in the
no-query browse view; `todo` is pulled out of Author to sit here once).
A quest row opens its hub (`/refs/quest/{id}`), a todo its own subtree
(`/tasks?focus={id}`) — both via `_OPEN_URL_OVERRIDES`. "🔁 Schedules"
is a preset link (`k=todo` + `tag=level:recurring`), not a kind — the
`level:recurring` value is a URL sentinel the route translates
server-side to `has_schedule=True` (§M retired the tag itself); Quests +
Schedules also sit in the nav Browse ▾ menu.
The no-query landing (`store.recent_refs`) orders by **`updated_at`** (not
`created_at`), so an edited artifact — a re-worked draft — bubbles back to
the top (`updated_at` bumps on real body/title/move/meta edits, not on
tag/flag or background embedding/summary writes, so the order tracks genuine
edits; the `sort=oldest` facet reverses it to least-recently-edited first).
It also shows only **unfiled** refs at the top level (`unfiled_only` →
`parent_id IS NULL`): a filed artifact drops out of the main list and lives
inside its folder, still reachable via search (which ignores folders).
Selecting a folder shows its direct children; the `state=deleted` trash view
shows every deleted ref regardless of filing. `unfiled_only` rides the shared
`_recent_refs_where` builder, so `count_recent_refs`'s "of N" denominator
hides the same filed refs the page does.
`cited_by=<draft>` scopes the `state=stub` acquisition queue to one draft's
**papers-to-fetch** set — the papers it cites but the corpus lacks (0 body
blocks), derived by `handlers/_citations_view.draft_fetch_ref_ids` and passed
as `store.recent_refs(ref_ids=…)`; the same to-fetch derivation as
`get(kind='draft', view='citations')`, so drive-scope and the view never
diverge. The `/smartdraft/{id}` Export tools link to it ("papers to fetch ▸").
Every bespoke list this replaced — `/items`, `/papers` (+`/papers/triage`),
`/drafts`, `/papers-needed`, `/refs/{oracle,patent}`, `/cfp` — is now a
307-redirect to a Drive kind/tag/state preset (e.g.
`/drive?k=paper&submitted=1`); each kind's **detail reader** (`/papers/{id}`,
…) is untouched — only the *list* retired (a draft opens in `/smartdraft/{id}`,
and the legacy `/drafts/{id}` reader path now 307-redirects there). The 🔍 loupe and
the flag-toggle bounce-back (`src/precis_web/routes/flags.py`) both
default to `/drive` now.

**Gripes workbench (`/gripes`, new).** `src/precis_web/routes/gripes.py`
is the first write surface for the dev bug tracker (`kind='gripe'`):
list (grouped by `STATUS`), detail + comment timeline, a status-change
POST (closed-vocab `tag` verb — `open → triaged → ready_for_fix →
in_review → wontfix`), and a `retire` (soft-delete, distinct from
`wontfix`). Nav badge counts every non-`wontfix` gripe
(`src/precis_web/nav.py::_gripes_count`).

**Categorizers console (`/categorizers`, new).**
`src/precis_web/routes/categorizers.py` lists every axis (`data/axes/*.yaml`)
and topic (`data/topics/*.yaml`) — granularity, prereqs, and a live
enable/disable toggle (`POST /categorizers/toggle`) writing the all-hosts
`service_config` row the axis's own `axis:<id>` service (or the shared
`classify`/`classify_topics` pass, for `role3`/`junk`/topics) reads — flips
in place, no redeploy. The `classify_topics` global kill-switch has its own
On/Off/Default strip (below the amber force-off banner) — Off force-disables
every topic, Default reverts to "runs if any topic is on". Each inventory
row carries its own coverage bar + a **last-run** timestamp
(`max(created_at)` folded into the same scan at zero extra cost — no new
query/index) inline; there's no separate Coverage section. Those two cells
are placeholders on the fast initial paint and get patched in by a single
deferred `GET /categorizers/progress` via htmx **out-of-band swaps**
(`id="lastproc-<name>"` / `id="cov-<name>"`, `hx-swap="none"` loader) — one
corpus-scan aggregate pass, N rows filled, still deferred off the paint
(mirroring `/status`'s backlog fragment). A `↻ refresh coverage` button
re-fires the same fetch on demand. Each row also renders static
outcome-**chips** (`_drive_chip_url`) deep-linking to `/drive` filtered by
that tag — one per topic (`topic:<slug>`), one per value on ref-level axes
(`<NS>:<value>`); chunk-level axes (role3/junk/open-question) get none
(their tags are on chunks, not papers).

**System — merged Status+Factory+Budget+Models
(`/status?tab=health|services|models|budget`).**
`src/precis_web/routes/status.py::index` dispatches on `tab=` to
`_health_ctx` (host/liveness strip, was `/status`), `_services_ctx` (the
old `/factory` category tables + live prio/model_pref edit + the ADR 0066
per-tier chain editor), `_models_ctx` (the `llm` catalog as read-only cards
— `_llm_card_view` splits Cloud from fleet-served Local by *model id* [ADR
0066: `tier_floor` is capability now, not location], topped by
`_active_routing_ctx`: a "what each capability tier dispatches to *right
now*" header derived from the live `resolve_chain`/`resolve_model` — binds
the store first so the operator-chain overrides are read, not the compiled
defaults), and `_budget_tote` (the spend cap/pause/resume controls, was
`/budget`).
`/factory` and `/budget`'s `GET` routes are bare redirects into the
matching sub-tab; their `POST` write routes (`/factory/prio`, `/budget/set`,
…) are unchanged — only the *page* merged, not the write paths.

## LLM-facing skill index

Lives under `src/precis/data/skills/precis-*-help.md`. Start at
`precis-toolpath-help` (canonical call sequences per scenario);
`precis-overview` has the master kinds table + skill index (it, plus
the synthesised `precis-help`, is the authoritative kind catalogue —
the README lists only a sample). Cross-refs: `precis-tasks-help`,
`precis-decomposition-help`, `precis-auto-tasks-help`,
`precis-recurring-help`, `precis-dispatch-help`, `precis-job-help`,
`precis-fix-gripe-help`, `precis-nursery-help`.

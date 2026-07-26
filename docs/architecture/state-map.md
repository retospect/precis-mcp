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
scheduling, execution, and review:

* **Hierarchy.** `parent_id` column on refs; a
  strategic / tactical / subtask gradient with walk-on-read ancestry
  and a 1/N rotation across strategics by 7-day picks. Reparenting
  goes through a reserved `parent` **link** relation (ADR 0027), not
  a raw column write.
* **`meta.auto_check` leaves.** Wait-for-condition evaluators under
  `auto_check_evaluators/`: `paper_ingested`, `discord_reply_received`,
  `time_past`, `tag_present`, `child_job_succeeded`.
* **`level:recurring` umbrella ("Watches").** `meta.schedule` (cron /
  `every:` shorthand for recurring, or a one-shot `at` timestamp — ADR
  0061) drives a per-minute spawner. Queue-mode ticks mint a
  `level:subtask` child (the original Slice-4 behaviour); a recurring
  carrying `meta.deliver={'target': ...}` instead fires a push
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
  owner decides retry / switch / give up.
* **Planner coroutines.** An `LLM:*`-tagged todo runs the `plan_tick`
  coroutine — each tick is a `kind='job'` that may mint children
  (`verdict: continue`) or yield (`ask-user:`) and still exit
  `STATUS:succeeded`. `child_job_succeeded` is guarded so it never
  auto-closes a parent that is `LLM:*`-tagged or still has a live
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
  since it excludes anything with open tags), and **worker health** (daemon liveness, not
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
  dead-worker to age). These three are the only
  `critical` categories — a thrashing/dead/stalled worker stalls the planner
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
* `structural` — opus, 6h dedup, agent profile. Drift, sibling
  contradictions, depth/fanout warnings. Dedup is symmetric: a **failed**
  dispatch (non-paused error — e.g. the agent container missing
  `PRECIS_DATABASE_URL` on a host) writes a `review-fail:<name>` cooldown
  marker so the pass backs off to `min_interval_hours` instead of
  re-dispatching every tick (was spinning spark to 124k ERROR/24h).
* `deep_review` — opus, weekly dedup, agent profile. Allen-style
  archive / prune / rebalance / long-wait review.

## Workers

**Service registry (the declarative source of truth).** Profile
membership + the extra `PRECIS_*_ENABLED` gates are declared once in
`src/precis/workers/registry.py` — a frozen `ServiceSpec` row per
pass/job-type/compute/daemon/serving (factory-console slice 1,
`docs/design/factory-console-and-scheduling.md`). `cli/worker.py`
derives `system_passes`/`agent_passes` via `service_names_for_profile()`
and folds the old inline `or env_flag(...)` gates into `_pass_enabled`
(reading `spec.enable_env`); the `/env` inspector derives its agent list
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

**Live run control — `service_config` (slice 2).** `service_config(host,
service, prio, model_pref, write_level, …)` (migration 0072) is the
DB-driven switch the worker consults *live* instead of a plist gate flag:
`prio 0` = off, `1..10` = claim weight (fed into the scarcity+prio+age
claim ordering slice 6 adds). An empty table is byte-identical to the
env/profile defaults; a row overrides per host (exact host wins over the
`*` wildcard). `workers/service_config.py::ServiceConfigResolver` (a
short-TTL cache) is read at boot (`_pass_enabled`) *and* per-cycle
(`run_loop`'s `pass_gate`), so a flip takes effect on the next cycle — no
redeploy. **Both directions, for the live-toggleable categorizer passes**
(`classify`, `classify_topics`, every `axis:<id>`): these are registered
*unconditionally* at boot (`_register_categorizer`, guarded only by `--only`),
so a live On-flip actually starts them — the per-cycle gate is the sole
enable/disable. The gate's no-row baseline is each service's real env/profile
default (`_gate_default_on`: an `axis:<id>` seeds from `PRECIS_AXES_ENABLED`,
everything else from its `ServiceSpec`), **not** a blanket "on" — so an
always-registered default-off pass stays off until a `prio>=1` row lands.
(Before: only `_pass_enabled` gated registration, so a default-off pass was
never in `ref_passes` and an On-flip was a silent no-op until a restart — the
`/categorizers` "activated but nothing happens" bug.) CLI: `precis service
prio|model|clear|list`.

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
(`workers/executors/_common.py`) orders `COALESCE(prio, 5) DESC, ref_id
ASC` (was pure `ORDER BY ref_id` FIFO), and `dispatch` mints each child
job with `prio = <parent todo's prio>` — so prio flows down the DAG and a
high-prio quest/project claims its compute ahead of commodity work,
oldest-first within a band. An all-unset queue is byte-identical to the
old FIFO. The capability-rarity term (§5.3, 6d) is not yet added.

**ssh_node crash recovery — lease-steal (claim-side).** `run_ssh_node_pass`
passes `reclaim_stale_running=True` to `claim_executor_jobs`, so a
`STATUS:running` job whose lease has *provably* expired (non-null and
`< now()`) is claimable again — its worker died mid-dispatch (a deploy
restart is the common cause; the ssh_node dispatch is in-process, so a dead
worker == dead compute). The steal bumps `meta.attempts`; past `_MAX_ATTEMPTS`
(3) it's failed + bubbled (poison-guard against a job that crashes its worker
every time), and a stolen job's stale `meta.reserved` slots are refunded
before it re-reserves. Opt-in per caller — `claude_inproc`/`coordinator` are
unchanged (they'd need their own ensure-dead story for a re-run). A live-lease
running job is never stolen. Container dispatchers (dft) must reap their own
handle before relaunch; catpath (in-process) has nothing to kill.

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

**The `sweeper` excludes `ssh_node`-executor jobs** (`meta.executor IS
DISTINCT FROM 'ssh_node'` on both its enumerate + transition-re-verify
queries): the sweeper fails an expired-lease `STATUS:running` job outright,
which would *race and win* the claim-side steal at lease expiry — stranding
the compute result as `failed` instead of retrying it. So the executor owns
crash-recovery for its own jobs; the sweeper still reaps every other
executor's (`claude_inproc` plan_tick, etc.).

**Two `precis worker` profiles, four LaunchDaemons total.**

* `precis worker --profile=system` runs on every cluster node and
  drives every chunk-level + SQL ref-level pass: `embed`, `summarize`,
  `chunk_keywords`, `chase`, `fetch`, `gp_fetch`, `tag_embeddings`,
  `auto_check`, `schedule`, `nursery`, `dispatch`, `sweeper`,
  `job_coordinator`, `job_ssh_node`, `wake_runner`, `clusterize`,
  `corpus_reconcile`, `paper_reconcile`.
  (`llm_summarize` is opt-in on top — env `PRECIS_SUMMARIZE_LLM=1` or
  `--only llm_summarize`; enabled on melchior as a deliberate trickle.
  `job_claude_docker` is opt-in on top too — env `PRECIS_SANDBOX_ENABLED=1`
  or `--only job_claude_docker`; default-OFF so the slice merges dark,
  meant only for the `agent_sandbox_host` nodes, **never melchior**.
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
* `dream_agent` keeps its own 15-min cadence via `dream-pass.sh`
  (each tick now injects a per-cycle **quest-anchor** nudge — a random
  active quest seeds one of the two anchors, `angle≈0.5`, other leg stays
  free; `PRECIS_DREAM_QUEST_ANCHOR`/`_ANGLE` — and opens a `kind='agentlog'`
  provenance node whose id rides `env_overlay` so its spawned websearch /
  memory refs attribute back via `touched`),
  and `cron-tick` is the fourth daemon — post-ADR-0061 it fires due
  `level:recurring` ticks (queue-mode spawn or `meta.deliver` push)
  via `run_schedule_pass`, not the retired `kind='cron'` engine. Each
  heavy pass dedups on its tier-tagged memory and load-gates on
  `PRECIS_LOAD_CEILING` (default `os.cpu_count() * 1.5`).

**Notable passes:**

* `cast_audio` — the daily audio **casts** (docs/design/reading-prep-loop.md
  §Audio). Two standing casts ride one produce→narrate→publish spine, two voice
  profiles: **`reading`** (morning situational-awareness brief, `bm_george`,
  ~20 min — `reading/briefing_cast.py` unions news/activity/recall/quest lanes, each
  degrade-to-empty; depth-first prompt, papers carry abstracts + a `[[pa<id>]]`
  cite marker (dropped inline per claim → a `§` link in `/drafts`, stripped from
  audio by `narrate.speakable`) + true overnight paper count (not the naming cap),
  leech cards carry bodies, active-only quest report with a decaying dormant nudge
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
  briefing now compose via the LLM router's `Tier.CLOUD_SUPER` (`DispatchClient`,
  ADR 0046) onto `claude_agent` — a `claude -p` subprocess, direct Anthropic OAuth —
  not the melchior-loopback litellm proxy; litellm now serves only the local tiers)
  on daily `level:recurring` watches; **TTS is the separate downstream spark pass**, so the
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
  cards (`represents`-linked, riding `precis anki-sync`). The brief's recall
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
  deploy-time claim orphans — **except `ssh_node`-executor jobs**, which
  the executor itself reclaims + retries (see the crash-recovery note above).
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
  See `docs/design/duplicate-paper-handling.md` (Phase 3).
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
timeout + structured `log_event` to `ref_events`. The reviewers,
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
transparently remaps the `LOCAL_SMALL` local-only aliases
(`summarizer`/`rake-lemma`) to a configured hosted small model
(`llm.model.local-small` override → `PRECIS_LOCAL_SMALL_HOSTED_MODEL` → default
`z-ai/glm-4.7-flash`) whenever the call lands on a hosted OSS transport
(`router.py::_hosted_small_remap`) — a no-op under default `anthropic` or a
local `served_by` slot. **Part 2** — the `openai_tools` loop now captures
OpenRouter's `usage.cost` (falling back to the token price table) into
`LlmResult.cost_usd`/`llm_call_log.cost_usd` (`openai_tools.py`,
`router.py::_dispatch_openai_tools`), so the budget breaker isn't blind to
OpenRouter spend. **Part 3** — `resolve_model(tier, backend=)`
(`router.py::resolve_model`) is backend-aware: under an effective `ANTHROPIC`
backend it drops an incoherent OSS `app_settings` model override for the
`CLOUD_*` tiers only (local-tier overrides are always honored — they never
route to a claude transport), so a half-applied flip (backend demoted for a
missing `PRECIS_LLM_BASE_URL`, model override still OSS) never hands an OSS
slug to a claude transport. The two raw-`claude`-subprocess sites —
`fix_gripe` and `sandbox_run`/`claude_docker` — read `resolve_backend()` and
skip clean (no spawn, job marked skipped/cancelled, not failed) under
`backend=openai` rather than being folded through `dispatch`. **Built: the
`FailoverProvider`/`Rung` ladder**
(`PRECIS_LLM_FAILOVER`, off by default) wraps an OSS primary transport with
an automatic claude-fallback rung on a transport error; a `LOCAL_*` tier's
claude rung resolves through `_LOCAL_ESCALATION_TIER` (its own
`_TIER_MODEL` default is an OSS alias, not a claude id — `LOCAL_BIG`
escalates to `CLOUD_MID`'s sonnet id; `LOCAL_SMALL` gets no claude rung at
all, per the roster below). The ladder also covers a **saturated local
slot**: `dispatch()`'s paused-slot branch (`local_serving.acquire()` returns
`paused=True`, all capacity busy) retries the ladder's rung 0 with the
request's `local_url` override cleared, landing on the hosted OSS endpoint
instead of the busy local hardware, before falling to claude if that also
errors (`docs/proposals/llm-openrouter-bypass.md` item 3). `Tier.LOCAL_SMALL`
also gained a `backend`-aware branch in `select_transport` (`OPENAI_COMPAT`
under `backend=openai`, item 2) — previously pinned unconditionally to the
loopback litellm proxy with no hosted-backend escape at all. **SMALL judge
pins reasoning off:** a tool-less `SMALL`/`LOCAL_SMALL` call with no explicit
`effort` merges `reasoning:{enabled:false}` into the `openai_compat` body
(`router.py::_dispatch_openai_compat`) — a reasoning model on that rung
(`z-ai/glm-4.7-flash`, tier-floor medium) otherwise spends the whole
`max_tokens` budget on a reasoning trace and returns empty `content`;
`LlmClient.complete` now normalizes null/omitted content to `""` (not the old
`str(None)`=`"None"`, a 4-char pseudo-answer that slipped past every
empty-check and silently failed while `errored` stayed false), so the caller's
empty-handling fires (classify no-value, summarize's `EmptySummaryError`
retry). **SMALL local calls also fail fast:** `_dispatch_local` caps a
`SMALL`/`LOCAL_SMALL` call's loopback timeout at `_SMALL_LOCAL_TIMEOUT_S` (30s,
vs the 120s `LlmConfig` default; an explicit `req.timeout_s` still wins) so a
stuck/flapping `:4000` proxy fails fast → the `FailoverProvider` falls over to
the hosted rung, instead of a batch (N chunks × 2 calls × 120s) blocking past
the worker watchdog and stranding the pass (the 2026-07-26 classify stall).
**ADR 0066 Phase A** (dark/additive, no live caller yet — call-site
sweep is Phase C):
`live_config.chain_override(tier)` + `router.py::resolve_chain` layer a
per-tier `app_settings`-backed chain override in front of the compiled
default. **Phase B (step 1, `resolve_chain` always-on):** `dispatch` /
`dispatch_async` now resolve *every* call through `resolve_chain`, so an
operator `llm.chain.<tier>` override is read **regardless of
`PRECIS_LLM_FAILOVER`** (Phase A wired it inside `if _failover_enabled():`,
which left a set chain inert). With no override, `resolve_chain` →
`_default_chain`: a single primary rung by default (byte-for-byte the
pre-Phase-B non-failover path), or `_failover_ladder` when the flag is on.
`dispatch` wraps in `FailoverProvider` iff `_failover_enabled() or len(chain)
> 1 or chain[0].model is not None` — reproducing the legacy flag-on/flag-off
wrapping exactly, and activating operator chains (every parsed override rung
pins a model, so a single-rung operator chain is honoured too). **Phase B
(step 3a, cloud throttle, §5):** `live_config.cloud_enabled()` (app_settings
`llm.cloud_enabled`, default true) + `router.py::_apply_cloud_throttle` prune
a resolved chain's cloud rungs when an operator disables cloud —
`_rung_is_cloud` classifies by explicit operator `placement` label, else by
transport (claude = cloud; litellm = local; OSS = cloud iff
`PRECIS_LLM_BASE_URL` set). A tier with a local rung keeps flowing on it; a
tier left with no rung prunes to empty → `dispatch` returns `paused`
(skip-not-fail, never silently degraded). **Which tiers survive depends on
their chain:** `FRONTIER` is always cloud-only (pauses); today only `SMALL`
has a standing local rung (`LITELLM`), so `BIG`/`MEDIUM` also pause under
throttle until an operator chain gives them a `placement:"local"` rung (the
target-state "drop to local" story lands with the Phase-3 roster / chain
editor). No-op while cloud is on (byte-identical). **Phase B (step 2,
operator surface):** `/status?tab=services` now carries an operator
placement-chain editor (a per-tier JSON textarea `POST /factory/llm/chain`
writing `llm.chain.<tier>`, server-validated list-only, blank = revert) plus
a cloud-throttle toggle (`POST /factory/llm/cloud` writing
`llm.cloud_enabled`) — `status.py::_llm_chain_ctx`, degrade-safe. This is the
write UI for the two settings the router already reads. Two Phase-B pieces are
**deferred to Phase C** for concrete code reasons: the `tier_floor` card
migration (step 3b) is clobbered by `llm_catalog.seed_default_cards`'
first-wins `for tier in Tier` on every reconcile tick, so it must ride with
the Phase-C catalog reseed; and the caller-picker→4-rungs split is entangled
with the unresolved planner-tag-vocab question (`LLM:small`/`medium` no-op via
fallback). The legacy GLM-preset panel (`_llm_override_ctx`) stays until
Phase C. `router.py::Tier` also gained four capability rungs
(`FRONTIER`/`BIG`/`MEDIUM`/`SMALL`) **additively** alongside the legacy five,
each routing byte-for-byte identically to its legacy analogue
(`FRONTIER↔CLOUD_SUPER`, `BIG↔CLOUD_MID`, `MEDIUM↔CLOUD_SMALL`,
`SMALL↔LOCAL_SMALL`), with new `LLM:` aliases `frontier`/`big`/`medium`/
`small` alongside the unchanged legacy aliases. **Failure semantics (§5a):** a
transport exception is classified (`router.py::_is_unavailability`) —
timeout / connection / HTTP 5xx / 429 → `paused` (skip-not-fail, the todo
retries next cycle, never parks); HTTP 4xx (semantic) stays `error`. Applies to
the OSS transports and, via `ClaudeProcessError.timed_out`, to a `claude`
wall-clock timeout too — so a claude-only rung (`FRONTIER`) waits rather than
parking. Every transport already carries a wall-clock timeout (claude 600 s,
openai_tools / litellm 120 s), so a hang converts to that classified failure.

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
- **Not yet built**: the quest-family synthesis tick body (harvest
  `topic:X`-tagged papers lacking an `integrated-into` link → merge into the
  topic's dossier `draft`), the weekly digest cast, and the daily-brief lane.
  Backlog: `OPEN-ITEMS.md` § "Topic dossiers (ADR 0060)". Full design:
  `docs/decisions/0060-topic-dossiers.md` + `docs/design/topic-dossiers.md`.

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
  `meta.cohorts`, `derived-from`→paper provenance). Remaining: graph-edge
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
  silently re-open ruled-out ground (the catpath dead-3-days spin; distinct
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
  the review+propose tick (`run_quest_tick`, tier `local-big` → the node-local
  OSS model), co-dispatches the batch's barrier/relax sims, and `Yield`s on an
  `at_time` heartbeat until they land — event-driven, self-paced on sim
  completion (not a cron), with per-quest backpressure (no new batch while one is
  in flight) + a node-load starvation gate. The coordinator claim now honours
  `meta.params.target_node` (`coordinator._claim_jobs` passes `PRECIS_NODE`) so
  the loop pins to the GPU/model node; catpath sims carry `resources.wall_seconds`
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
  The **barrier lane mirrors this** (§C completed): a failed `catpath_explore`
  is *always* a crashed NEB (a compute failure, never a physical "no pathway"
  verdict), so it retries once (`meta.quest_catpath_infra_retries`) then gripes
  (`lane="catpath"`) and **never** rules out — `_latest_catpath_job` +
  `dispatch_catpath` re-dispatch, same retry-once-then-gripe shape as relax.
  **Barrier quality gate** (`compute._pathway_quality`, gr172323): harvest
  lifts not only the scalar `barrier`/`span` but a trust verdict read from the
  linked pathway's `meta.warnings` — `barrier_trusted=False` iff any
  `NEB not converged` or adsorbate-`detached` warning. An untrusted barrier is
  **excluded from ranking** (dropped from `measures` in
  `frontier._candidate_from_structure` → the candidate lands `unevaluated`,
  never on the frontier; the raw value survives as
  `flags.barrier_untrusted_value` for the leaderboard's `⚠ non-converged` /
  `(excluded)` cell) and can **never graduate** (`graduate.py` belt-and-
  suspenders gate on `_CATPATH_GATED_KEYS`). NB single-seed runs make catpath's
  own `low_confidence` uninformative (always `n<2`), so the *warnings* are the
  signal, not that flag — the physical fix (endpoint desorption pre-flight,
  bigger NEB budget, seed ensemble) is catpath-side + quest-config, tracked in
  gr172323.
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
  5 min and *not renewed mid-slice*, so a live-but-slow `local-big`
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
  escalates one tier to `Tier.CLOUD_SUPER` and re-prompts once more; still
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
  constants (`router._TIER_MODEL` + the `LLM:opus|sonnet|haiku|local` tag) into a
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
  cheap (`CLOUD_SMALL`) one-shot judge that infers a *capability requirement*
  (never a model name — the LLM is price/window-blind + self-biased), and
  `choose_model(store, task)` chains it into `select_offering`. Every field is
  clamped so a malformed reply can't produce an illegal requirement; the judge is
  injectable for tests. Plus the agent-facing `precis-llm-help` skill (express a
  requirement, don't pick a model) and CLI `precis llm choose`. **All 5 slices
  built + green** (facts → guardrail → ledger → policy → agent surface); ship dark.
- **`alert`** — machine-detected ops/health conditions (spin loops, orphans),
  raised via `precis.alerts.raise_alert` (fingerprint upsert + auto-resolve),
  read via `AlertHandler`/`/alerts`. **Not embedded.** Skill: `precis-alert-help`.
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
  only; harvest is slice 2). See `docs/design/sandbox-run.md`. Skill:
  `precis-job-help`.
- **`structure`** — atomistic cell+bond IR (ADR 0043); typed ops + in-memory
  probes, relax on the GPU node (derived-lane job, ADR 0044), cursors/measures
  on `struct_measures`, web `/structure`. `slab` op hardened against messy LLM
  JSON (null/list params → clean `OpError`, not a crash); `invariants.py` gives a
  representation-invariant fingerprint (composition · per-layer · adsorbate site ·
  coordination) powering the **round-trip eval** (`scripts/llm_eval/roundtrip.py`,
  `docs/design/structure-roundtrip-eval.md`). `structure_propose` build step
  pinned to CLOUD_MID=sonnet (ties opus at ½ cost; reasoning stays super). Skill:
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
    `quest/compute.py::dispatch_catpath` **mints no job** for a failing substrate
    and stamps a `ruled-out:preflight` dead-end the proposer reads. Catches
    *authoring* faults (badly-placed / spongiform / out-of-box); *physical*
    desorption of a well-authored slab stays the catpath-tier (MLIP) verdict.
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
  Two-pane paper reader (`routes/papers.py` + vendored pdf.js); the **draft
  reader** (`routes/drafts.py`) is a true virtual scroller for 10k-block drafts
  (skeleton + windowed DOM, no IntersectionObserver — see git log for the
  feedback-loop lesson). `precis_web` is a sibling package over the handlers (ADR 0026).
  **Export can bundle the cited sources** (`export/sources.py`,
  `collect_cited_sources`/`build_sources_zip`): the reader's `+ sources`
  checkbox appends every cited paper/datasheet PDF the host holds to the PDF as
  a `pdfpages` appendix (`export_draft(include_sources=True)`) — Word gets a zip
  (`report.docx` + `sources/`) since it can't embed PDF pages — and
  `GET /drafts/{id}/papers.zip` (also `precis draft papers`) zips just the cited
  PDFs + a `manifest.txt`. PDFs resolve via the same corpus resolver as
  `corpus_reconcile` (`corpus_layout.rebase_onto_local`); the corpus being
  per-host, unlocatable sources are listed in the manifest rather than failing.
  **Review surface** (paper-writing-pipeline `chunk_review` ledger,
  `OPEN-ITEMS.md` § "Topic dossiers"): a ✓ gutter checkoff per block
  (`POST /drafts/{id}/human-review` → `edit(review='human')`, one-way), a
  read-only per-chunk F/C/S/A checker-flag strip mirroring
  `view='review'` (✓ current / ~ stale / – unreviewed), a machine-authored
  marker for grounded-authoring-reviewer edits, and a toolbar toggle for
  the per-draft `authoring_enabled` flag (`edit(kind='draft',
  authoring=)`).
- **SSRF guard** — `src/precis/utils/safe_fetch.py` (used by `handlers/web.py`
  + `workers/fetch_oa.py`); DNS-resolves + revalidates every redirect against the
  private/loopback/link-local/cloud-metadata blocklist.
- **Ingest hygiene** — pysbd sentence splitter in the chunker fallback chain;
  dehyphenation in `marker._clean_text`; HNSW index on `chunk_embeddings.vector`.
- **`asa-slack`** — Slack bridge sibling to `asa_bot` (`src/asa_slack/`), Socket
  Mode. Routes each turn through the ADR-0046 router (`Tier.CLOUD_MID` — sonnet
  forced) via a single blocking `dispatch()` call, no live progress ticker —
  asa_bot's own Discord bridge also routes through the router now
  (router-migration Phase 3, `asa_bot/claude_invoke.py`: `Tier.CLOUD_SUPER`,
  streaming `dispatch_async` + `on_event` so the Discord progress indicator
  still ticks live), so both bridges get the budget breaker/route-log for
  free. A hard kind-allowlist (`asa_slack/kind_policy.py`, via
  `LlmRequest.env_overlay`'s `PRECIS_KINDS_DISABLED`) restricts Slack turns to
  research lookups + `memory` — `job`/`quest`/`cron`/`todo` unreachable, not
  just prompt-discouraged. Every conversation is a thread (never the channel
  root); capture is unconditional (every message asa sees, human or bot, plus
  its own replies); per-person memory reuses `asa_bot.preamble.build()`'s
  existing `user:<handle>` mechanism unchanged. ADR: `docs/decisions/0062-asa-slack-bridge.md`.

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
(`q=`, kind/tag facets, `sort=relevance|recency`, `since/until`,
`state=stub`, pagination) grafted onto Drive's folder tree (`_flatten_tree`)
+ CRUD (`POST /drive/new|create|{id}/rename|move|{id}/delete`) + per-row
quick actions (`ItemPresenter.actions()`, `src/precis_web/item_view.py`).
The kind chips are three facet rows: **Source** (chunk-searchable corpus),
**Author** (`role='artifact'`, foldered), and **Work** (`_WORK_KINDS =
quest, todo` — agenda kinds with no body chunks, so they list in the
no-query browse view; `todo` is pulled out of Author to sit here once).
A quest row opens its hub (`/refs/quest/{id}`), a todo its own subtree
(`/tasks?focus={id}`) — both via `_OPEN_URL_OVERRIDES`. "🔁 Schedules"
is a preset link (`k=todo` + `tag=level:recurring`), not a kind; Quests +
Schedules also sit in the nav Browse ▾ menu.
Every bespoke list this replaced — `/items`, `/papers` (+`/papers/triage`),
`/drafts`, `/papers-needed`, `/refs/{oracle,patent}`, `/cfp` — is now a
307-redirect to a Drive kind/tag/state preset (e.g.
`/drive?k=paper&submitted=1`); each kind's **detail reader** (`/papers/{id}`,
`/drafts/{id}`, …) is untouched — only the *list* retired. The 🔍 loupe and
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
in place, no redeploy. Coverage %s are a lazy `GET /categorizers/progress`
htmx fragment (the corpus-scan aggregates are deferred off the initial
paint, mirroring `/status`'s backlog fragment).

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

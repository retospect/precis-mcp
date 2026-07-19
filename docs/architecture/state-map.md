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
* **`level:recurring` umbrella ("Watches").** `meta.schedule` (cron
  or `every:` shorthand) drives a per-minute spawner. `PRIO` is an
  int column on refs (1..10); `PRIO:*` tag stays as a back-compat
  alias.
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
  env `PRECIS_PLAN_TICK_RESUME_CAP`) past which it bubbles as a real
  failure (the task needs splitting).
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

* `nursery` — SQL-only, every minute on the system worker. Flags
  orphans, stale claims, long waits, stuck doable, stalled recurrings,
  **spin loops** (any `(ref_id, source)` emitting >
  `SPIN_LOOP_EVENTS_24H` (200) `ref_events` in 24h), **plan-tick
  spins** (a planner parent minting > `PLAN_TICK_REMINT_24H` (16)
  `plan_tick` jobs in 24h — the coroutine "succeeds" each tick but never
  converges, which the resume-streak cap doesn't catch since it only
  guards exhaustion loops), and **worker health** (daemon liveness, not
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
gone). `tests/test_worker_registry.py` AST-parses `cli/worker.py` and
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
(`run_loop`'s `pass_gate`), so a flip disables an already-registered pass
on the next cycle — no redeploy. CLI: `precis service prio|model|clear|list`.

**Console — `/factory` (slice 3, read-only).** `precis_web/routes/factory.py`
renders a host strip (`host_heartbeat` load + liveness) over one list per
category of every registry service, joined to its live `service_config`
prio and its last-ok/last-fail from `worker_logs` (keyed by the
`BatchResult.handler` string via `ServiceSpec.log_handler`). Each section
degrades to empty on a schema surprise (the status-tab pattern); agent
rows link to the `/env` inspector. **Slice 4 (live edit):** a host
selector scopes the page; each row's prio is editable (POST `/factory/prio`)
and model-using rows get a model_pref dropdown (POST `/factory/model`)
populated from the `llm` catalog — both write `service_config` straight,
picked up next cycle.

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
  meant only for the `agent_sandbox_host` nodes, **never melchior**.)
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
* `dream_agent` keeps its own 15-min cadence via `dream-pass.sh`,
  and `cron-tick` is the fourth daemon. Each heavy pass dedups on its
  tier-tagged memory and load-gates on `PRECIS_LOAD_CEILING` (default
  `os.cpu_count() * 1.5`).

**Notable passes:**

* `cast_audio` — the daily audio **casts** (docs/design/reading-prep-loop.md
  §Audio). Two standing casts ride one produce→narrate→publish spine, two voice
  profiles: **`reading`** (morning situational-awareness brief, `bm_george`,
  ~20 min — `reading/briefing_cast.py` unions news/activity/recall/quest lanes, each
  degrade-to-empty; depth-first prompt, papers carry abstracts + leech cards carry
  bodies, active-only quest report with a decaying dormant nudge; papers/findings
  `cites`, news wire `derived-from`, drafts/quests `related-to`) and **`nidra`**
  (evening concept-graph meditation, `af_nicole`, ~45 min segmented walk —
  `reading/meditation.py`; walked concepts `related-to`). Producers
  persist a standalone dated `draft` marked `meta.cast` and **link it back to the
  sources it drew on** via the shared `cast_common.link_sources` (a cast names its
  sources but reads no URL aloud, so the edge is the only durable pointer back —
  `links_for` the cast draft reopens them; best-effort, a bad edge is skipped); `workers/cast_audio.py`
  (spark, default-OFF `PRECIS_CAST_AUDIO_ENABLED` + `PRECIS_TTS_IMAGE`) narrates
  any un-narrated cast draft via `render_narration` → `render_episode` →
  `publish_episode(source=profile.source)` — a **distinct** producer tag per cast
  (`brief` / `meditation`, so a shared feed can subfilter), idempotent on `meta.audio_episode_id`
  (sibling to `briefing_audio`). Compose is the `reading_brief`/`meditation`
  **`claude_inproc`** job_types (melchior — both casts compose with `claude-opus`
  via the melchior-loopback litellm proxy, same host as the news briefing) on daily
  `level:recurring` watches; **TTS is the separate downstream spark pass**, so the
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
(`workers/executors/agent_container.py`), but the opt-in is now gated behind a
**verified-capability probe** (`container_capability_ok()`: auth token
resolvable ∧ `<bin> info` ∧ `<bin> image inspect` — per-process ~60s-cached,
fail-safe to in-proc), so an opted-in host that can't actually containerize
runs in-process instead of failing every pass. A containerized run's
**infra** failure (image-missing/daemon-unreachable/socket-perm/**OOM 137**,
vs. a claude/model error) trips a ~10-min health latch (`trip_container_unhealthy`)
and retries the same call in-process once — catching the OOM 137 here keeps it
off the router's `interrupted` (`rc>=128`) skip path. Flag stays opt-in
(unset=OFF); auto-detect retirement + `/factory` degraded-render are follow-ons
(`OPEN-ITEMS.md §🔇`).

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
prompt block is its sole steer there.) Still direct `claude -p`:
`fix_gripe`. Deferred: a `FailoverProvider` ladder (method + model
failover) over the same port.

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
  / `--only classify`). Manual backfill + eval: `scripts/classify/classify
  --cascade` (dry-run default; `--commit` to write). Full design:
  `docs/design/chunk-classifier-cascade.md`.

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
  (arrives with the loop, slice 4). **Slice 2 (reweighting) live**
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
  `precis-quest-help`.
- **`llm`** — the model catalog (design-of-record `docs/proposals/llm-catalog.md`;
  slice 1 **live, read-only, ships dark**). Turns model choice from hardcoded
  constants (`router._TIER_MODEL` + the `LLM:opus|sonnet|haiku` tag) into a
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
  sweeper GCs past `PRECIS_AGENTLOG_RETENTION_DAYS`. Skill: `precis-agentlog-help`.
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
- **SSRF guard** — `src/precis/utils/safe_fetch.py` (used by `handlers/web.py`
  + `workers/fetch_oa.py`); DNS-resolves + revalidates every redirect against the
  private/loopback/link-local/cloud-metadata blocklist.
- **Ingest hygiene** — pysbd sentence splitter in the chunker fallback chain;
  dehyphenation in `marker._clean_text`; HNSW index on `chunk_embeddings.vector`.

## LLM-facing skill index

Lives under `src/precis/data/skills/precis-*-help.md`. Start at
`precis-toolpath-help` (canonical call sequences per scenario);
`precis-overview` has the master kinds table + skill index (it, plus
the synthesised `precis-help`, is the authoritative kind catalogue —
the README lists only a sample). Cross-refs: `precis-tasks-help`,
`precis-decomposition-help`, `precis-auto-tasks-help`,
`precis-recurring-help`, `precis-dispatch-help`, `precis-job-help`,
`precis-fix-gripe-help`, `precis-nursery-help`.

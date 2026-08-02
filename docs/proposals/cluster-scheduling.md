---
status: draft
title: Cluster consolidation — one worker, one scheduler, one substrate; containerized, monitorable, elastic
model: opus
---

# Cluster consolidation (unified master plan)

> **The one plan to review.** This subsumes and supersedes the scheduling
> framing that was scattered across `factory-console-and-scheduling.md` §15,
> `gpu-priority.md`, `gpu-cluster-modes.md`, `health-watchdog.md`, and a dozen
> `OPEN-ITEMS.md` workstreams (Dark-factory Tracks 1–3, Worker-liveness, Budget
> guardrails, LLM-catalog, Quest layer). Those remain the **mechanical
> sub-specs**; this is the frame, the north-star, the honest current state, and
> the cross-cutting phasing. Greenfield where legacy is messy — we are not tied
> to the existing shapes, only to what's already *proven live* (noted as such).

## North star — the end state

By the time this is done, a precis host runs **four managed things and nothing
else**:

- **Postgres** — the one substrate. All state, all work, all coordination.
  **Runs precisely once** (the data node); the true singleton everything derives
  from.
- **The web** — the reader **and** the management surface (the `/factory`
  console): what's scheduled, what's running where, what each thing costs, and
  every live knob. **Runs precisely once** (the gateway).
- **The worker** — **one `precis worker` per host**, **thin**: it claims ready
  work, *dispatches it into containers*, runs only cheap passes in-process,
  manages leases — **and spins model-servers up and down on demand** (below). Its
  behaviour on a host is set by **capability × `prio`**, not by which daemons and
  env-flags happen to be set there.
- **asa** — the chat bridges (Discord/Slack), stdio to `precis serve`.

Two cardinalities, and only two: **Postgres and web run exactly once**
(cluster-wide singletons — substrate and surface); **the worker runs once per
host** (identical binary, per-host capability × `prio`). Nothing else is a
standing precis daemon.

Everything else that is a *daemon* today — dream, cron-tick, watch-poll,
anki-sync, reconcile, the four parallel worker profiles, the standalone review
timers — collapses into **a pass in the one worker's loop** or **a container the
worker dispatches to**. The **model-servers (bge-m3 embedder, llama.cpp/
llama-swap) are spun up and down by the thin worker on demand** — *not* standing
plists. They may run on the metal (mlock, direct GPU, no per-call container cost)
or in a container, but either way their **lifecycle is worker-managed, keyed to
demand**: the worker starts a server when a backlog + a free slot call for it and
tears it down when the backlog drains (§F). This retires the standalone
`embedder` and `embedder-watchdog` plists. The container runtime (colima/podman)
and the infra (pgbouncer, redis) are sidecars, not precis daemons.

The control surface flips completely: **from ~20 `PRECIS_*_ENABLED` env flags +
N launchd/systemd plists → one claim substrate + `service_config.prio` ×
capability-probe, live-tunable from the web.** "Turn a pass on" becomes "set its
prio in the console," not "edit a plist and redeploy." That single change is
what makes the fleet simultaneously *simpler*, *monitorable*, and *elastic*.

**One user changes everything.** There is effectively one human, so none of this
needs a multi-class fairness scheduler — fair-share, gang scheduling,
bin-packing, mid-kernel preemption are all out of scope by construction. What
remains is small.

## The seven design laws (the spine)

Every axis below is an application of these; if a proposed mechanism violates
one, it's wrong.

1. **One substrate.** precis already has exactly one scheduling substrate: the
   decentralized derived claim queue (ADR 0007/0017). Workers *pull* ready work
   with `FOR UPDATE SKIP LOCKED`; a claim is a **reserve-at-claim** conditional
   advance (`UPDATE … WHERE still_available RETURNING`) — the advance *is* the
   lock. Every "scheduler" concern is a **policy on that one claim**, never a
   second system.
2. **One scheduler.** One lease-backed recurring-clock (`scheduler` pass,
   `scheduler_leases`) folds *every* cadence — mint a job when `next_fire_at ≤
   now()`, run by every worker, exactly-once via the conditional-advance lease.
   No standalone timers, no bespoke per-producer `app_state` throttles, no
   designated node.
3. **One control surface.** A pass runs on a host iff the host has the
   capability and `service_config.prio` says so — set live from the web. Retire
   the `PRECIS_*_ENABLED` env matrix and the plist-per-daemon model.
4. **The worker is thin; *work* runs in containers it dispatches to.** Isolation,
   OS-portability (one image across launchd + systemd hosts), and clean teardown
   all fall out. Cheap CPU passes may run in-process; anything heavy, agentic,
   GPU-bound, or crash-prone runs in a dispatched container. **Two carve-outs:**
   (a) **Postgres and web are standing singletons** — the substrate and the
   surface, run precisely once, not units of work; (b) **model-servers
   (llama.cpp/llama-swap, embedder) are worker-spun on demand** — the worker
   starts and stops them keyed to backlog + slots (§F), whether on the metal
   (mlock/GPU) or in a container. Serving is elastic and worker-managed, *not* a
   permanent daemon.
5. **Resumable, not killable.** Decompose work small and make each unit
   idempotent or content-addressed, so **interruption is free**: a lease expires,
   another worker re-claims, and it resumes or skips completed sub-units. You
   **kill a container, never a worker.** Force-kill of a compute is a last-resort
   escape hatch, not a responsiveness lever — and even then the work resumes
   automatically because it is content-addressed.
6. **Correctness in Postgres, never in a host.** Exactly-once, liveness, and
   resource reclamation live in the DB or in a verified reclaim — never in a
   designated host. A host being down must never drop a fire, wedge a unit, or
   stall a cadence. This is what makes a decentralized, single-user cluster safe
   with no scheduler daemon.
7. **One user ⇒ no fairness scheduler.** See above; the hard parts of cluster
   scheduling are out of scope.

| Concern | Policy on the one claim | Law | Pillar / sub-spec |
|---|---|---|---|
| **When** recurring work fires | conditional-advance lease **on time** | 2, 6 | P1 · §A |
| **Which** ready unit a worker takes | the **sort** in the claim query (`prio`) | 1 | P3 · §B-2 |
| **Whether** heavy background may start | a dispatch **gate** (reserve mode) | 1, 5 | P3 · §B-2 |
| **Where** a unit runs | **capability-reserved** claim (agentic→agent host, GPU→GPU host) | 3, 4 | P1 (built) |
| **In what** a unit runs | a **dispatched container**, not the worker process | 4 | P2 · §H |
| **How** interruption is clean | **resume/skip** (idempotent + content-addressed) + kill-the-container | 5 | P2 · §H, §B-1 |
| **How much** runs at once + **whether to spin up** | **counted-slot** reserve (`resource_slots`, `free -= 1`) + demand **batch-materializer** | 1 | P3 · §F |
| **How** the GPUs are shaped | pull-based **hysteretic mode switch** (fuse/split) | 1 | P3 · §C |
| **That** it's alive (or *correctly idle*) | **outcome-based** liveness digest over backlog/freshness | 6 | P4 · §D |
| **How often / how costly** a producer may be | a **live `service_config`/`app_settings` knob** (web→DB→pass), DB>env>default | 3 | P4/P5 · §G |

## Current state — honestly (built / live / dark / drift)

Reviewers need this to trust the phasing. Verified against the deploy roles,
`registry.py`, `scheduler.py`, and the ansible import graph on 2026-08-02.

- **The claim substrate is live and load-bearing** — all four executors, the
  capability-reserved claim, `mint_child_job` copying parent `prio`. This is the
  ground we build on.
- **`resource_slots` exists (migration 0073), half-wired.** Per-host `(host,
  resource, capacity, free)` rows (`gpu`/`podman`/`tts`), populated by the
  heartbeat self-probe, reserve pattern specified. **Only the LLM path
  (`local_serving.py`) consumes the counter** — slice 6c (consume at the
  *executor* claim) is unbuilt. §F finishes it.
- **The `scheduler` pass is LIVE in prod (verified 2026-08-02, read-only
  probe).** `PRECIS_SCHEDULER_ENABLED=1` on the deployed worker; the `cron-tick`
  and `watch-poll` plists are retired; both cadences (`cron_tick` 60s,
  `watch_poll` 3600s) are firing on schedule via `scheduler_leases`; the
  `schedule` pass runs on all four hosts. **The fold works — no drift.** Two
  consequences: (1) the in-repo "ships DARK / off by default" comments
  (`registry.py`, `scheduler.py`, `cli/worker.py`) are **stale and must be
  corrected** — they nearly caused this plan to mis-state current state; (2) §A is
  *not* "finish + flip" — the flip is done. What remains for §A is folding the
  **still-standalone** cadences (`dream`, `reconcile`, `anki_sync`, `heartbeat`)
  via host-affinity, and killing their plists.
- **The collapsed-worker north-star is already scaffolded — dark.**
  `deploy/playbooks/20b-precis-worker-collapsed.yml` renders ONE `precis worker`
  per host via the `service_unit` role, carrying **none** of the
  `PRECIS_*_ENABLED` toggles — control becomes capability-probe ×
  `service_config.prio` from the console. Plus `retire-thin-timers.yml` and
  `roles/service_unit/examples/collapsed-worker.yml`. **Not imported by
  `site.yml`** (runs in-window after the run-as→deploy cutover). P1 = *finish and
  apply this*, not invent it.
- **Containerized dispatch exists in pieces, dark.** `job_claude_docker`
  (`PRECIS_SANDBOX_ENABLED`, requires podman), the `com.precis.colima` sidecar,
  `code-sandbox`, and the `plan_tick`/`fix_gripe` spawn seams that build their
  own `claude -p` argv outside the `call_claude_agent` chokepoint. The "sandbox
  substrate" is flagged in the backlog as the *durable north star* for
  de-SPOF+isolation. §H makes it the default execution path for heavy/agentic
  work.
- **Daemons still standalone that P1 must fold:** `dream` (gateway, 15-min
  `StartCalendarInterval`, wrapper `dream-pass.sh`), `reconcile` (caspar, daily,
  single-host), `anki_sync` (own host group, 30-min), `heartbeat` (all nodes,
  60s), `embedder-watchdog`. Plus the standing daemons the picture must account
  for: `embedder`, `watch` (PDF ingestor), `web`, `asa_bot`/`asa_slack`, colima.
- **~20 env-gated passes** (`classify`, `inbound_chase`, `hub_refine`,
  `backlog_groom`, `cast_audio`, `mail_poll`, …) form the "flag mess" that P1's
  control-surface flip and §D's Layer-2 coherence check both target.
- **Cost is dominated by one producer.** `dream` (claude_agent/Opus) is **78.6%
  of all cluster LLM spend (~$46/day, $322/7d)**, on a hardcoded 15-min timer
  with no runtime knob. §G is the near-term cost fix.

## The pillars

Each pillar realizes the laws; each names the build-units (§-axes, kept as the
granular labels the sub-specs and acceptance criteria reference) and folds the
harvested backlog. **Two near-term standalone wins ship ahead of the frame:**
§B-1 (the one live *correctness* bug) and §G's dream throttle (the one live
*cost* bug, ~$35/day, no redeploy).

### Pillar 1 — One worker, one scheduler, one control surface (laws 1–3)

Collapse ~15 daemons into the one worker; fold every cadence into the one
scheduler; replace the env-flag matrix with capability × `prio`.

- **§A — Extend the (already-live) recurring-clock to *every* cadence.** The
  two-cadence flip is **done and live in prod** (cron_tick + watch_poll ride the
  scheduler; the timers are retired). §A's remaining work: (1) **fold the
  still-standalone cadences** — the north-star says one scheduler, so `anki_sync`,
  `heartbeat`, and even `dream`/`reconcile` become scheduler cadences via a
  **host-affinity** field on `Cadence` (dream stays melchior-pinned, reconcile
  caspar-pinned — as *affinity*, not a separate daemon), retiring `dream-pass.sh`
  and the dream/reconcile/anki/heartbeat plists outright; (2) **correct the stale
  "ships DARK" comments** in `registry.py`/`scheduler.py`/`cli/worker.py` (the
  pass is on). `catch_up` already fires late-not-lost on recovery; a fire is
  dropped only if the *entire fleet* is down.
- **§L — The collapsed-worker cutover.** Apply `20b-precis-worker-collapsed.yml`:
  one `precis worker` per host, no `PRECIS_*_ENABLED` blocks, behaviour = the
  capability probe × `service_config.prio`. Make it **OS-agnostic** — the
  restart/manage story must cover **systemd on the Linux/GPU nodes**, not just
  macOS launchd (`gr180078`); harden the bounce so *every* managed pass restarts
  on deploy (`gr…` bounce-coverage gap) and teardown reaps its subprocess
  (`gr171254`, `gr176337`); move the cleartext prod DB password out of the plists
  into vault (`gr171431`).
- **§E — Retire the bespoke `app_state` throttles.** The in-loop `last_run` +
  advisory-lock throttles (paper_reconcile, llm_reconcile, backlog_groom,
  corpus_reconcile, clusterize) each re-implement "run every N, single-flight."
  Once §A is proven, migrate them onto the one scheduler lease and declare each
  cadence once (its `ServiceSpec`). Pure de-duplication — lowest priority, last.

*Folds:* §15i, Track 3 (`service_unit` collapse, tooltips/per-host errors),
"Retire the cron-tick timer", `gr180078`, `gr171254`, `gr176337`, `gr171431`,
`gr172390` (embedder-venv build flakiness), the bounce-coverage gap, "deploy
doesn't disable the watcher on excluded hosts". *Sub-spec:*
`factory-console-and-scheduling.md` §15.

### Pillar 2 — Containerized dispatch + a resumable job lifecycle (laws 4–6)

The clean answer to "no killable things." The worker becomes a thin
claim-and-dispatch loop; work runs in containers; interruption is resume/skip.

- **§H — Containerized dispatch as the default for heavy/agentic/GPU work.**
  Generalize the dark `job_claude_docker`/sandbox pieces into *the* execution
  path: the worker claims a unit and runs it in an ephemeral container it
  dispatches to (local podman, or a remote node). Route the `plan_tick` +
  `fix_gripe` spawn seams through the one `call_claude_agent` chokepoint so every
  dispatch is logged and containerized uniformly. This dissolves the **melchior
  SPOF** (any capable host can run an agentic container) and the **co-location
  jetsam** (the 73 G-mlock'd weight lives in its own container, not co-resident
  with the worker). *Greenfield note:* this is the backlog's "sandbox substrate —
  the durable north star"; treat it as a clean build, not a patch of the spawn
  seams.
- **§H-lifecycle — lease is the single job-substrate liveness authority.** Make
  the reclaim path take over a `running` unit whose lease expired
  (requeue-from-checkpoint), and **retire the sweeper's `PRECIS_STUCK_JOB_HOURS`
  wall-clock** (Tier B). Add:
  - a per-process **`boot_id`/epoch on the running-job lease** so a bounced worker
    reclaims its own dead predecessor's units on the first claim pass instead of
    waiting out a 2h lease (`compute-lane-lease-epoch.md`);
  - **liveness-aware reclaim** — `reclaim_stale_running` checks whether the holder
    is actually alive (heartbeat), not just `lease_until < now()`, so detection
    doesn't lag a real kill by ~1h;
  - a **per-unit attempt cap** distinguishing killed-by-restart from genuine
    crash-loop, so a redeploy mid-run doesn't burn the poison guard
    (`ssh_node` deploy-kill class);
  - **child-deadlock guard** — a parent todo never blocks forever on a
    never-completing child job (the morning-brief SPOF class).
- **§B-1 — Fix the spark GPU wedge (the one live violation of law 5).** FIRST,
  it's a live bug. `autocatpath_explore` runs the whole NO→NH₃ network × 3 seeds
  × full NEB as one ~90-min in-process un-interruptible CUDA blob that overruns
  its lease and takes the worker down (SIGTERM-deaf → SIGKILL; 81 starts / 0
  completions, `gr180096`). **The fix is not a better kill — it's law 5:** fan out
  per `(model, seed)` → content-addressed jobs → `aggregate_partials`, each seed a
  small resumable container unit; a killed seed loses only that seed and a
  content-addressed retry skips completed seeds. **Operational stop-gap ACTIVE:**
  `quest:164903` set `STATUS:dormant` so spark stops re-wedging — **reverting it
  to `STATUS:active` is a required step of shipping §B-1** (a dormant quest left
  dormant is silently-stopped research). Detail: `gpu-priority.md` Phase 1 +
  `autocatpath-integration.md` §3.8.

- **§M — Normalize the work-item ontology (the "subthingies").** Audited
  (2026-08-02, grounded in the code): **the collapse is ~80% already done.** A
  todo is *one* `kind='todo'` whose "types" are `level:*` tags + `meta` facets
  (`executor`, `schedule`, `auto_check`, `workspace`, `deliver`) on the shared
  `refs` row (only `parent_id` + `prio` are dedicated columns); ADR 0044 already
  collapsed the job intent-vs-compute lane into a facet *derived from the parent's
  kind* — the exact "type = policy, not a new kind" move the design laws want.
  Project = "a strategic root that owns `meta.workspace`", explicitly no new kind.
  Views are read-lenses, not node types. **So the tree is not a zoo of kinds — it
  is one faceted kind with residual marker-tags that never got normalized.** The
  concrete, low-risk, forward-only deliverable:
  - **Collapse the `level:` 3-enum → 2 orthogonal bits.** strategic/tactical/
    subtask encodes only *is-rotation-root?* and *is-worker-mintable?*; "tactical"
    carries no unique mechanism (it's parent-depth by another name). Make the two
    bits explicit fields.
  - **Demote marker-tags to policy fields.** `level:recurring` is redundant with
    "has `meta.schedule`"; `LLM:*` is an auto-close gate expressible as a field.
  - **Document the facet model** so the next "type" is a field, not a new tag:
    parent (tree) · lifecycle-state (`STATUS:`) · `prio` · rotation-root /
    owner-mintable · cadence (`schedule`+`deliver`) · wait-condition
    (`auto_check`) · executor+resource (`meta.executor`/`job_type`/`requires`,
    §F) · intent-vs-compute lane (parent-derived). The LLM surface needs **no**
    back-compat aliases (`interface-is-free, data-isn't`); only stored refs, web
    routes, and ~10 nursery detectors/views take the forward migration.
- **The one boundary that stays: todo ↔ job.** This is the single *genuine* kind
  distinction, defended on **physical** grounds (ADR 0030's mechanisms test), not
  accident: a job is a *claimed, leased, executor-run* row (`FOR UPDATE SKIP
  LOCKED`, `idem_key` dedup, sweeper crash-recovery, lease-steal, resource slots
  §F); a todo is *durable intent that is never leased.* Merging them forces either
  row-lock contention (holding a todo lock for a multi-minute tick) or two state
  machines on one ref — "worse than two kinds." **Rule it out explicitly** rather
  than drift into it. This is precisely *why* Pillar 2's lifecycle work lives on
  the job side: the lease substrate is the job's whole reason to be a distinct
  kind. ("turn-as-job" is therefore a job-side move — every agent turn a *leased*
  work unit — not a todo↔job merge.)

*Folds:* "Tier B lease-as-liveness", `compute-lane-lease-epoch.md`,
`ssh_node`-deploy-kill (`OPEN-ITEMS` critical), `reclaim_stale_running` liveness
gap, morning-brief child-deadlock, `gr180096`, the sandbox-substrate items,
"containerize the plan_tick/fix_gripe spawn seams", the autocatpath
harvest-bookmark concurrency edge, `gr1821xx` (7 spark sim infra-failures — the
retry/resume class), the "turn-as-job routing + context DSL" and "natural state =
many pending todos → triage" backlog items.

### Pillar 3 — Elastic cluster resources on demand (law 1 applied to scarcity)

The incoming hardware makes this central. "Allocate cluster resources on demand"
= counted slots + demand-materialized batches + topology modes.

- **§F — Demand-materialized elastic work + counted resource slots.** Most
  background work is *not urgent and not standing* — it need only run when there's
  a backlog, as fast as the scarce resource allows.
  - **Count → threshold-batch-mint.** A cheap periodic demand-count mints a
    *batch* of low-`prio` jobs when a backlog crosses a threshold (e.g. >500
    unembedded chunks → mint the next 5000). Below threshold, mint nothing;
    drained, the claim returns empty and the work "finishes instantaneously." The
    hysteresis coalesces churn into few large batches.
  - **Counted slot at claim (finish slice 6c).** A job declares
    `requires={'gpu':1}` (or `llm:<model>`); the claim decrements the slot in the
    *same* transaction as the reserve-at-claim advance and releases on terminal
    (crash-reclaimed by the lease sweep). "1 GPU slot" then means exactly that,
    cluster-wide, no dispatcher. Generalize `local_serving`'s `llm:<model>`
    acquire/release to any `resource_slots` row; seed rows by the host's real
    llama-swap model ids so slot-gating doesn't silently no-op to litellm.
  - **The daemons — and the model-servers — dissolve into this.** The worker
    **starts the model-server on demand** and tears it down when the backlog
    drains: the embedder becomes a slot-bounded batch-drainer that spins bge-m3 up
    *for the batch* then releases it (cold load amortized across the batch, RAM/GPU
    freed when drained) — no standing `embedder`/`embedder-watchdog` daemon.
    "Local deep thinking" is the same shape at higher `prio` with an
    `llm:<local-model>` slot: the worker brings the local model up when there's a
    batch or an urgent request, drains, and releases. This is what "spun up and
    down by the thin worker on demand" means concretely — serving lifecycle keyed
    to the same demand-count + slot signal as the work itself.
- **§B-2 — Priority-claim + reserve mode (kill demoted).** Human responsiveness,
  single-user-simple, **resumable-first**:
  - **Human-first claim.** The claim already orders on `refs.prio`
    (`_common.py::claim_executor_jobs`, `ORDER BY COALESCE(r.prio,5) DESC`), and
    `mint_child_job` copies parent `prio` — but the direction *looks inverted*
    vs the `0014` convention (lower = more urgent). §B-2 is a **correctness
    reconciliation** of that live sort (pin the direction with a test), not
    greenfield.
  - **Reserve mode** — a TTL'd `service_config` flag the dispatch gate reads: stop
    minting/claiming *new heavy background*; the in-flight unit finishes cleanly
    and the box is the human's. This is the **primary** responsiveness lever.
  - **Kill backstop — last resort only.** For a genuinely wedged
    non-interruptible compute, force-kill with verified GPU reclamation
    (`kill_container`+`reset_gpu`). Per law 5, this should be *rare* — the wedge is
    fixed by chunking (§B-1) and by container teardown (§H), not by routine kills.
- **§C — GPU topology modes (fuse vs split) — gated.** For the incoming 3 Sparks
  (+1 today): fuse N units into one big accelerator vs split for many jobs, as a
  pull-based hysteretic mode switch. **Gated on the honest counter:** one Spark's
  ~119 GB already serves ~120 B @ 8-bit / ~200 B @ 4-bit quantized; fusion earns
  its complexity *only* for frontier-size (~400 B+) you cannot quantize onto one
  unit, and the ConnectX/RDMA fabric makes a fused pool a **batch** engine, not
  interactive. Run the one-Spark-quantized test first; if your models fit,
  shelve §C. Detail: `gpu-cluster-modes.md`.
- **§I — De-SPOF + co-location relief (largely delivered by §H).** Provision a
  second agent-capable host (the sparks help); get the mlock'd weight off the
  agent host. Mostly falls out of containerized dispatch, but track the ops
  provisioning explicitly.

*Folds:* §5.5 slice-6c, `gpu-priority.md`, `gpu-cluster-modes.md`, `gr162694`
(console `resource_slots` self-probe render), Track 2 (`served_by` seeding, slot
contention), `gr175799` (local-slot saturation), the de-SPOF / co-location /
sandbox items, `gr51393` (local pre-flight before cloud DFT), spark provisioning
(nvidia docker runtime, `torch-cuda` base image).

### Pillar 4 — Monitorable: liveness net + management surface (law 6's observability)

The way to manage everything above — no producer silently stops and rots.

- **§D — Liveness net.** A periodic **outcome-based** digest that reaches out,
  escalates with age, and routes each finding onto a standing fix-path. SLA is
  forgiving ("never urgent, just don't let it rot for days") — the failure it
  guards is slow (the 4-day worker-agent outage; `chunk_keywords` dead 26 d).
  Composes with nursery (the fast critical-page lane), SQL-first so it doesn't
  depend on the fleet it watches. **Coupled to §F:** once producers evaporate
  when idle, liveness must read the *same backlog signal* and alarm only on
  *backlog-present-but-not-draining*, never on quiet — **one liveness truth** from
  the one registry (`ServiceSpec × service_config × worker_logs`) plus the backlog
  count. Full spec: `health-watchdog.md`.
- **§K — Factory console v2 (the web management surface).** The `/factory`
  console is where the new control surface *lives*: per-scheduled-task "next run",
  per-host "last error", per-host machine-profile / `resource_slots` self-probe,
  and the live `prio` knobs that replace the env flags. This is `gr162694` +
  Track-3 console work; it is the human-facing half of laws 2–3.
- **External dead-man's-switch.** An out-of-band `SELECT 1` watcher on a
  *different* host → Discord (+ a worker-log-volume trend alarm) — the only
  signal that survives a *total* fleet/DB outage (the ~8h prod outage went
  unalerted because every alerting path was DB-backed). Plus set
  `PRECIS_OPS_ALERT_TARGET` (nursery's critical push is dark until it is).

*Folds:* `health-watchdog.md`, `gr162694`, "Out-of-band DB-liveness monitor",
`/checklogs`, the "ops guy" reasonableness-read agent, config-drift guard
(deployed plists vs rendered templates), "detect an env-gated pass silently
absent from a live rotation", `gr162141` (openalex-balance alert — verify
closeable).

### Pillar 5 — Cost governance & routing (law 3 applied to spend)

- **§G — Live control + the dream throttle (near-term cost win).** Ship-now:
  mirror the budget-breaker pattern — keep the 15-min plist dumb, add a
  `dream.min_interval_minutes` knob (default 15; **no migration**, `app_settings`
  exists), the dream pass no-ops if too soon, beside its `skip_if_high_load`
  gate. Bump to 60 on the budget tab → ~4× fewer dreams (~$46→~$11/day), live, no
  redeploy. The **reusable pattern** (web form → DB knob → self-throttling pass,
  DB>env>default) makes *every* cadence in the frame a live knob. (Once §A folds
  dream into the one scheduler, this knob becomes a `service_config` cadence field
  — same lever, cleaner home.)
- **Cost observability + capture.** Add per-producer / per-run cost attribution
  (join `llm_call_log.ref_id` onto job refs) so the knob-turner sees *which*
  producer to throttle — and **fix the OpenRouter `cost=null` blindness**
  (`gr171782`): the `openai_tools` path logs no cost, so the breaker can't meter
  OpenRouter spend at all. Surface the cost-band affordance to model prompts
  (Budget Piece A); the `service_calls` rollup for non-LLM spark compute is a
  later add, only if compute (not LLM) becomes the constraint.
- **Routing / cheap-tiering.** Push mechanical work (summarize, triage children,
  CI-fix) to small local/cheap models; reserve Opus for judgment. The
  **local-first capacity valve** (run local, spill to cloud on saturation with
  the *same* model so spill is quality-invisible) and the **proprietary/local-only
  routing guard** (a must-stay-local tag + a guard refusing to assemble a cloud
  prompt containing a tagged ref) both land here. Consider dropping the
  `PRECIS_LLM_BACKEND` enum entirely — infer transport from the resolved model id.

*Folds:* `dreamtransfer.md`, Budget guardrails (Piece A/C), `gr171782`
(OpenRouter cost capture), `gr175799` (saturation→hosted-fallback spend),
`local-first-capacity-valve.md`, proprietary-local routing, cheap-model tiering,
the `is_paid(tier)`-gate SMALL-band budget bug (`OPEN-ITEMS` LLM-routing).

### Pillar 6 — Guarded autonomy (the auto-fix ladder)

The §D remediation router can climb from *nudge* to *auto-fix* one rung at a
time, each earning trust: Rung 0 file-gripe → **Rung 1 auto-draft, human-ship**
(unattended reproduce→coder→gate→reviewer→ready-to-`/go` branch; 0% autonomous
deploy) → Rung 2 auto-ship a whitelisted narrow class behind post-deploy verify +
auto-rollback → Rung 3 widen. Safety spine every rung: reproduce-first (red
test), the `scripts/ship` gate, reviewer sign-off, post-deploy outcome re-check.

- Runs on **§H's container substrate** (the sandbox) — that's the dependency the
  health-watchdog spec names.
- **Injection safety (`gr179498`) is a Rung-1 prerequisite:** the existing
  `fix_gripe` rail runs `claude -p --dangerously-skip-permissions` on *verbatim
  gripe text* — an untrusted-input → full-privilege-agent surface. Before climbing
  any rung, the rail must treat gripe/finding text as data (sandboxed, no
  ambient prod credentials), not as trusted instructions.

*Folds:* `health-watchdog.md` §2b, the ADR-0048 fixer-loop residuals,
`gr179498`, `sandbox-run-substrate`.

## Hardware forcing function — the incoming 3-Spark cluster

**The cluster today** (authoritative inventory: the `git_deploy_helper` ansible
repo — cite it as the physical substrate, don't duplicate deploy config here):

| Node | Role | Notable |
|---|---|---|
| **melchior** | gateway · agent worker · web · asa | OAuth/`claude_inproc` (today's SPOF); the 73 G mlock'd weight co-locates here |
| **spark** (DGX) | inference · agent worker · GPU compute | cores 0–1 fenced for the system (`precis_compute_cpuaffinity/job_cpuset: "2-19"`); **`podman_gpu_passthrough` + `podman_sandbox_user: agent_sandbox` already provisioned** — GPU-in-container is a solved substrate here (corroborates §H) |
| **caspar** | Postgres · NFS · redis | the run-once substrate node; `reconcile`'s single-host pin |
| **balthazar** | small Mac · local model slot | the SMALL-tier local model host |

The core-fence (reserve 0–1 for the system, compute on 2–19) is exactly law 5 in
the OS: heavy compute can't starve the worker's own responsiveness. `+3` Sparks
extend the inference/GPU/agent tier — more `gpu` slots, more agent-capable hosts
(the melchior de-SPOF), more `agent_sandbox` container capacity.

1 DGX Spark today, 3 incoming. This is *why* Pillar 3 (and its coupling to
Pillar 4) moves from "later" to central:

- **Disaggregated (the default):** N independent single-node `gpu` slots draining
  many small jobs (catpath seeds, DFT/relax, embedding, per-node models). This is
  the elastic-slots story (§F) at cluster scale — and the *only* mode you need if
  the one-Spark-quantized test passes.
- **Aggregated (gated, maybe never):** fuse the units into one `bigpool` slot for
  a frontier model too big to quantize onto one unit — **batch, not interactive**,
  because the fabric is Ethernet/RDMA not NVLink (§C). Start with a manual
  `precis cluster fuse`/`split`; earn autonomy only if manual proves tedious.
- **Provisioning debt to clear first:** nvidia docker runtime not configured by
  ansible on spark (breaks Marker OCR fleet-wide), no `torch-cuda` base image
  mirror, and the OS-agnostic worker-manage story (§L / `gr180078`) — the sparks
  are Linux/systemd, so "one worker" must not be macOS-launchd-only.

The sparks also relieve the **melchior SPOF** (a second agent-capable host) and,
via §H containerization, the **co-location jetsam** — so Pillars 2–4 compound on
this hardware rather than each needing bespoke work.

## Roadmap / phasing (cross-pillar, urgent-first)

1. **Near-term standalone wins (independently shippable now):**
   - §B-1 spark wedge (correctness) — + revert `quest:164903` to `active`.
   - §G dream throttle (cost, ~$35/day, no redeploy).
2. **P1 — collapse to one worker / one scheduler / one control surface.** The
   scheduler is already live (verified); remaining §A folds the standalone
   cadences (dream/reconcile/anki/heartbeat via host-affinity) + corrects the
   stale comments; §L applies the collapsed-worker cutover (env-flags→`prio`,
   OS-agnostic manage across launchd + systemd).
3. **P2 lifecycle** (§H containerized dispatch + lease-as-liveness + epoch +
   liveness-aware reclaim) — the resumability substrate; de-SPOFs melchior as a
   side effect. **§M's marker-tag normalization can land independently and early**
   (it's small + forward-only, the audit is done) — do it before §H hardens
   shapes, but it needs no design spike now.
4. **P3 elastic** (§F slice-6c + materializer alongside §B-2 prio/reserve; §C
   gated on the quantized test) — needs the lease substrate proven (after §A) and
   the slots wired.
5. **P4 monitorable** (§D Layer 1 anytime; §K console; dead-man's-switch; §D
   Layer 2 with/after §F for the shared backlog signal).
6. **P5 cost/routing** beyond §G; **P6 autonomy** last (depends on §H sandbox +
   `gr179498`).
7. **§E throttle-consolidation** only after §A is proven live ≥1 week.

## Acceptance criteria

- **§B-1:** `dispatch_autocatpath` mints seed-per-job + aggregate tree; a killed
  seed loses only that seed and a content-addressed retry skips completed seeds;
  the worker stays SIGTERM-responsive; the aggregate yields the same scalar
  barrier harvested today.
- **§A:** with the scheduler on fleet-wide and folded plists removed, each folded
  cadence (incl. dream/reconcile/anki via host-affinity) fires **exactly once per
  interval across the fleet** — no double-fire during overlap, no dropped fire
  when the previously-owning host is down; `catch_up` fires late-not-lost.
- **§L:** one `precis worker` per host with no `PRECIS_*_ENABLED` blocks behaves
  identically to today's per-flag matrix, driven by capability × `service_config.
  prio`; a `prio` change in the console takes effect within one claim cycle with
  no redeploy; the manage/restart path works on **both** launchd and systemd;
  every managed pass restarts on deploy; no cleartext DB password in any plist.
- **§H:** a heavy/agentic unit runs in a dispatched container; tearing the
  container down leaves the worker process alive and the unit re-claimable; an
  agentic unit runs on a host that is *not* melchior (SPOF gone); the mlock'd
  weight is not co-resident with the worker.
- **§H-lifecycle:** a bounced worker reclaims its own dead predecessor's units on
  the first claim pass (not after a 2h lease); a *live* holder is never stolen; a
  redeploy mid-run does not burn the poison guard; a parent never blocks forever
  on a never-completing child.
- **§B-2:** a human-`PRIO:`-urgent unit is claimed ahead of background, with the
  sort direction reconciled to the `0014` convention (a test pins it); reserve
  mode stops new heavy dispatch within one claim cycle and auto-expires on TTL;
  force-kill is exercised only in the injected-hang drill and reclaims the GPU.
- **§F:** a backlog above threshold mints a bounded low-`prio` batch and no more
  until it drains; a unit declaring `requires={'gpu':1}` cannot be claimed when
  `free=0` (injected two-claim race leaves one queued); the slot releases on
  terminal and is reclaimed on a crashed holder; embeddings fully drain as
  batched slot-gated jobs; the resident model is warm for a batch and released
  after.
- **§C:** `fuse` drains to a low-water mark, stands up the pool, `bigpool` jobs
  run; `split` tears down with no orphaned reservation; modes are mutually
  exclusive; a dead pool node releases the whole pool and requeues its jobs.
- **§D:** a deliberately-stopped cadence shows stale within its interval+margin; a
  *correctly-idle* demand producer (empty backlog) does **not** alarm while a
  non-draining backlog **does**, both from the same signal; the digest still
  sends (templated) when the LLM/fleet is down; the external dead-man's-switch
  fires on a total-fleet outage.
- **§G:** setting `dream.min_interval_minutes=60` no-ops dream passes inside the
  interval within one cadence, no redeploy, ~4× fewer real dreams in
  `llm_call_log`; the knob resolves DB>env>default (unset = byte-identical 15-min
  behavior); per-producer cost attributes `claude_agent` spend by source; the
  OpenRouter path logs a non-null cost.
- **§K:** the `/factory` console shows, per host, each scheduled cadence's
  next-run + last-error + `resource_slots` free/capacity, and setting a pass's
  `prio` there changes its scheduling within one claim cycle (shares §L's
  live-knob path) — the management surface for laws 2–3.
- **§M:** the `level:` 3-enum is replaced by two explicit fields (rotation-root,
  owner-mintable) and `level:recurring`/`LLM:*` by policy fields, with a
  forward-only data migration and no LLM-surface alias; the todo↔job kind
  boundary is unchanged (a test pins that a job row still leases and a todo never
  does).
- **§E:** each migrated throttle fires on the same cadence, single-flight
  preserved, interval declared in one place; no behaviour change beyond the tick
  source.

## Explicitly NOT in scope

- **Multi-class fairness scheduling** — fair-share, gang scheduling, mid-run
  yield, memory-aware bin-packing: moot with one user. Escalate per
  `gpu-priority.md`'s deferred appendix only if contention persists after §B-1.
- **A dispatcher / singleton scheduler daemon** — the claim substrate needs none.
- **Interactive low-latency serving from the fused pool** — interconnect makes it
  batch; serve interactive models per-node.
- **Routine force-kill as a responsiveness lever** — law 5; reserve+drain and
  container teardown are primary, force-kill is the rare escape.

## Open questions / decisions log

- **Split vs single doc (resolved 2026-08-01→02, Reto):** one unified master
  (this doc); `gpu-priority.md` / `gpu-cluster-modes.md` / `health-watchdog.md` /
  `factory-console-and-scheduling.md` remain the mechanical sub-specs.
- **North-star = "worker + web + asa + postgres" (Reto, 2026-08-02):** the
  managed precis units. Postgres + web are standing bare-metal singletons (run
  once); container runtime + infra are sidecars.
- **One scheduler (Reto, 2026-08-02):** fold *every* cadence incl.
  dream/reconcile/anki via host-affinity — revises the earlier "keep
  dream/reconcile standalone" stance. Host-pinning is a cadence *affinity*, not a
  reason for a separate daemon.
- **Resumable, not killable (Reto, 2026-08-02):** design work small + idempotent/
  content-addressed so interruption is free; kill containers, not workers;
  force-kill is last-resort. Reshapes §B-2 (reserve-first, kill demoted).
- **Containerized dispatch in scope (Reto, 2026-08-02):** the worker dispatches
  to containers (§H); this is the substrate for clean teardown, de-SPOF, and the
  autonomy sandbox. Greenfield-clean where the spawn seams are messy.
- **Model-servers worker-spun on demand (Reto, 2026-08-02):** law-4 carve-out —
  llama-swap/embedder are *not* standing daemons; the worker starts/stops them
  keyed to demand + slots (§F), on the metal (mlock/GPU) or in a container.
  Retires the standalone `embedder`/`embedder-watchdog` plists. Only Postgres +
  web are standing bare-metal singletons.
- **Work-item ontology — AUDITED (2026-08-02).** The todo tree is already ~80% one
  faceted `kind='todo'` (tags + `meta`), and ADR 0044 already collapsed the job
  lane. §M's real deliverable is narrow and forward-only: normalize the residual
  marker-tags (`level:` 3-enum → 2 bits; demote `level:recurring`/`LLM:*` to
  fields) + document the facet model. **Decision needed:** ratify that the
  todo↔job boundary **stays** (ADR 0030 physical grounds) — the one collapse to
  rule out, not drift into.
- **Scheduler-flag drift — RESOLVED (verified live 2026-08-02).** The scheduler
  pass is on in prod, timers retired, both cadences firing. §A is "extend to the
  rest," not "flip." Residual: the in-repo "ships DARK" comments are stale — fix
  in the §A commit.
- **§A host-affinity representation** — a per-`Cadence` host field vs a `prio`
  cell on the lease. Pin before building §A.
- **§H boundary** — which passes containerize first (agentic + GPU are the clear
  wins; cheap CPU passes may stay in-proc). And: reuse the `job_claude_docker`
  seam or rebuild clean per the greenfield license?
- **§F materializer placement** — one generic pass reading each producer's
  `(count-query, threshold, batch-size, resource)` from its `ServiceSpec`, vs
  each producer minting its own batch.
- **§F reshape vs leave-standing (per-producer)** — `embed` already drains
  correctly; reshaping buys uniformity at the cost of churn. Clear §F wins are
  slot-gating the GPU/local-model producers and elastic residency.
- **§C — is fusion even needed?** Gate on the one-Spark-quantized test.
- **§G knob shape** — cadence (`min_interval_minutes`, recommended) vs daily cap
  vs on/off; and bundle-or-ship-first the cost view.
- **§D↔§F shared liveness (resolved 2026-08-02, Reto):** one liveness truth, one
  registry + one backlog signal; recorded in `health-watchdog.md`'s frame
  blockquote too. Confirm the shared signal before §D Layer 2.
- **Housekeeping (not this plan):** `gr162141` and `gr55762` are self-described as
  shipped but still `STATUS:open` — verify + close, don't fold.

### ADR 0048 readiness pass (2026-08-01, retained verbatim)

**Resolved into the body (2026-08-01):** the blocker and the `news_poll` advisory
are corrected in §B-2 (human-first claim rewritten as verify-and-reconcile of the
live, seemingly-inverted `_common.py` prio sort; blast radius = the shared claim
SQL) and §A (`news_poll` removed from the launchd-fold list — it's an in-loop
pass). Findings kept as the audit trail:

- **blocker — §B-2 claim-order Target was wrong and the criterion may already be
  inverted.** `_common.py::claim_executor_jobs` does `ORDER BY COALESCE(r.prio,5)
  DESC` on `refs.prio` (live, all four executors), `mint_child_job` copies
  `parent_prio`; the `0014`/`handlers/todo.py` convention is *lower = more
  urgent*, opposite of the `DESC` — so a `PRIO:urgent` (prio=1) unit may sort
  *behind* background today. §B-2 is a reconciliation of a live sort, not "one
  comparison." (Now reflected above.)
- **advisory — §A `news_poll` misdescribed** — no `precis_news_poll` plist/role
  exists; it's an in-loop pass, nothing to fold. (Now reflected.)
- **advisory — split-into-siblings** — not re-flagged; §B-1 is independently
  shippable and phased first.

All other code-grounded claims checked out (the dark `scheduler` pass + `CADENCES`
+ flag, `Store.claim_scheduler_lease`, the `PRIO:` axis, `struct_relax`
`kill_container`/`reset_gpu`, `dispatch_autocatpath`/`harvest_measures`,
`child_job_succeeded`, the five `app_state` throttles, the capability-reserved
claim design, `autocatpath`'s `run_one_seed`/`aggregate_partials`, and all
referenced sub-specs).

## Relationship to the sub-specs (reconciliation)

This doc is the index + ordering + north-star; it duplicates no mechanics.
Precedence when detail conflicts: the sub-spec wins on mechanics, this doc on
cross-axis ordering and the design laws. Pointers, both directions:
`factory-console-and-scheduling.md` §15 → P1 (§A/§E/§L) + §K,
`gpu-priority.md` → §B, `gpu-cluster-modes.md` → §C, `health-watchdog.md` → §D,
`compute-lane-lease-epoch.md` → §H, `local-first-capacity-valve.md` → Pillar 5,
`gpu-priority.md` Phase 1 ↔ `autocatpath-integration.md` §3.8 (build of record
for §B-1). Full wedge trail: `gr180096`.

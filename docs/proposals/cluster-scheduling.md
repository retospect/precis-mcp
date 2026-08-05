---
status: draft
title: Cluster consolidation — one worker, one scheduler, one substrate; containerized, monitorable, elastic
model: opus
---

# Cluster consolidation (unified master plan)

> **The one plan to review.** Subsumes the scheduling framing previously
> scattered across `factory-console-and-scheduling.md` §15, `gpu-priority.md`,
> `gpu-cluster-modes.md`, `health-watchdog.md`, and the related `OPEN-ITEMS.md`
> workstreams. Those remain the mechanical sub-specs (reconciliation at the
> end); this doc is the frame, the target state, the honest current state, and
> the ordering. Greenfield where legacy is messy — tied only to what is already
> proven live.

## The system, stated straight

1. **Postgres is the only coordination substrate.** A claim is a
   reserve-at-claim conditional update (`UPDATE … WHERE still_available
   RETURNING`) — the decrement *is* the lock. Every scheduling concern is a
   policy on that one claim, never a second system.
2. **Three cluster-wide singletons** — Postgres (the data node), web (the
   gateway; reader + `/factory` management console), asa (the chat bridge, one
   process per chat surface) — **plus one thin worker per host.** Nothing else
   is a standing precis daemon.
3. **One scheduler.** A lease-backed recurring clock folds *every* cadence;
   exactly-once via the conditional-advance lease; no standalone timers, no
   designated node.
4. **Control = capability probe × `service_config.prio`,** set live from the
   web. "Turn a pass on" is a console knob, not a plist edit + redeploy. The
   ~20 `PRECIS_*_ENABLED` env flags and the plist-per-daemon model retire.
5. **Heavy work runs in per-workload pinned containers** the worker dispatches
   to; cheap CPU passes may stay in-process. You kill a container, never a
   worker.
6. **Interruption is free** because units are small and idempotent /
   content-addressed: a lease expires, another worker re-claims, work resumes
   or skips what's done.
7. **Resources are counted slots** in Postgres; background work is
   batch-minted on demand; model servers are spun up for a batch and released
   when it drains — elastic serving, not standing daemons.
8. **Liveness is one outcome-based digest** over backlog/freshness (alarm on
   backlog-present-but-not-draining, never on quiet); **cost is a live
   per-producer knob.** Versioned artifacts live in **git**; Postgres indexes
   them.

**One human user ⇒ no fairness scheduler.** Fair-share, gang scheduling,
bin-packing, mid-kernel preemption are out of scope by construction. That is
what keeps the whole thing small.

Everything below elaborates these sentences: the laws they generate, what
already exists, the build-units (§A–§M — labels kept stable because sub-specs
cite them), and the ship order.

## North star — what a host runs

A precis host runs **four managed things and nothing else**:

- **Postgres** — all state, all work, all coordination. Runs once (caspar).
  Versioned *bytes* live in git on the NAS; PG holds the searchable index +
  pointers (see "Files & artifacts").
- **The web** — reader + the `/factory` console: what's scheduled, what's
  running where, what it costs, every live knob. Runs once (gateway).
- **The worker** — one `precis worker` per host, thin: claims ready work,
  dispatches it into containers, runs cheap passes in-process, manages leases,
  and spins model-servers up/down on demand (§F). Its behaviour on a host is
  capability × `prio`, not which daemons and env flags happen to be set there.
- **asa** — the Discord/Slack bridges, stdio to `precis serve`. Runs once per
  chat surface (gateway).

Every *daemon* beyond these — dream, cron-tick, watch-poll, anki-sync,
reconcile, the four parallel worker profiles, embedder + embedder-watchdog —
collapses into a pass in the one worker's loop or a container it dispatches.
Model servers (bge-m3, llama-swap) are **worker-spun on demand**: started when
backlog + a free slot call for one, torn down when the backlog drains, whether
on the metal (mlock, direct GPU) or in a container. The container runtime
(colima/podman) and infra (pgbouncer, redis) are sidecars, not precis daemons.

## The seven design laws (the spine)

Every mechanism below is an application of these; a proposal that violates one
is wrong.

1. **One substrate.** The decentralized derived claim queue (ADR 0007/0017);
   reserve-at-claim; every scheduler concern is a policy on the claim.
2. **One scheduler.** The lease-backed recurring clock (`scheduler` pass,
   `scheduler_leases`) folds every cadence.
3. **One control surface.** A pass runs on a host iff capability ×
   `service_config.prio` says so — live from the web.
4. **The worker is thin; work runs in dispatched containers.** Two carve-outs:
   (a) the three singletons are standing infrastructure, not units of work;
   (b) model-servers are worker-spun on demand (§F), not permanent daemons.
5. **Resumable, not killable.** Small idempotent/content-addressed units;
   kill the container, never the worker; force-kill is a rare escape hatch,
   and even then work resumes because it is content-addressed.
6. **Correctness in Postgres, never in a host.** Exactly-once, liveness, and
   resource reclamation live in the DB or a verified reclaim. A host being
   down must never drop a fire, wedge a unit, or stall a cadence — with one
   deliberate exception: an **affinity-pinned** cadence stalls while its
   pinned host is down, by design (see §A's carve-out).
7. **One user ⇒ no fairness scheduler.**

**Failure vocabulary** (the precise senses laws 2/6 and the acceptance
criteria turn on):

- **Fire** — one scheduled occurrence of a cadence. *Dropping* a fire = it
  never runs at all (distinct from late — `catch_up` is late-not-lost).
- **Unit** — one claimed, leased `kind='job'` row. *Wedging* a unit = stuck
  non-terminal indefinitely, holding its lease + any reserved slot.
- **Cadence** — a recurring schedule. *Stalling* a cadence = it stops emitting
  future fires (e.g. the lease owner dies mid-advance) — kills every
  subsequent occurrence, not one.

| Concern | Policy on the one claim | Law | Where |
|---|---|---|---|
| **When** recurring work fires | conditional-advance lease on time | 2, 6 | §A |
| **Which** ready unit a worker takes | the sort in the claim query (`prio`) | 1 | §B-2 |
| **Whether** heavy background may start | a dispatch gate (reserve mode) | 1, 5 | §B-2 |
| **Where** a unit runs | capability-reserved claim | 3, 4 | live |
| **In what** a unit runs | a dispatched container | 4 | §H |
| **How** interruption is clean | resume/skip + kill-the-container | 5 | §H, §B-1 |
| **How much** at once + spin-up | counted slots + demand batch-mint | 1 | §F |
| **How** the GPUs are shaped | hysteretic fuse/split (gated) | 1 | §C |
| **That** it's alive (or correctly idle) | outcome digest over backlog/freshness | 6 | §D |
| **How often / costly** a producer runs | live web→DB knob, DB>env>default | 3 | §G |

## Current state — honestly

Verified against the deploy roles, `registry.py`, `scheduler.py`, and the
ansible import graph, 2026-08-02. Per-mechanism status lives in **one place**:
the moving-pieces index below (live / dark / spec). Fleet-level facts:

- **The claim substrate is live and load-bearing** — all four executors, the
  capability-reserved claim, `mint_child_job` copying parent `prio`.
- **`resource_slots` (migration 0073) is half-wired.** The **hardware half is
  built, wired, and test-covered**: the executor claim reserves
  `gpu`/`podman`/`tts` slots in the claim txn and refunds on terminal,
  self-gating on host advertisement (`test_reserve_at_claim.py`) — confirm a
  prod host actually advertises `gpu` before leaning on it live. The **LLM
  half is dark**: `llm:<model>` slots no-op until a card carries `served_by`.
  Deferred (6d): scarcity re-rank + soft mem-pressure veto.
- **The `scheduler` pass is live in prod**: `cron_tick` + `watch_poll` ride it,
  their timers are retired, cadences fire on schedule fleet-wide. (In-repo
  "ships DARK" comments are stale — corrected in the §A commit.) §A's
  remaining work is folding the three still-standalone **fleet-singleton**
  cadences: `dream` (gateway, 15-min), `reconcile` (caspar, daily),
  `anki_sync` (30-min). `heartbeat` (60 s) also loses its plist but is
  **per-host, not a scheduler cadence** (see §A); `embedder-watchdog` §F
  retires outright.
- **The collapsed worker is scaffolded, dark.**
  `deploy/playbooks/20b-precis-worker-collapsed.yml` renders one flag-free
  `precis worker` per host; not yet imported by `site.yml`. §L applies it.
- **Containerized dispatch exists in pieces, dark**: `job_claude_docker`
  (podman-gated), the colima sidecar, `sandbox_run` (`mode:build` only), and
  the `plan_tick`/`fix_gripe` spawn seams that bypass the `call_claude_agent`
  chokepoint. §H makes this the default path for heavy/agentic work.
- **Cost is dominated by one producer**: `dream` (claude_agent/Opus) is ~79 %
  of all cluster LLM spend (~$46/day) on a hardcoded 15-min timer with no
  runtime knob. §G is the near-term fix.

## The pillars

Each pillar realizes the laws and names its build-units. Two standalone wins
ship ahead of the frame: **§B-1** (the one live correctness bug) and **§G's
dream throttle** (the one live cost bug).

### Pillar 1 — One worker, one scheduler, one control surface (laws 1–3)

- **§A — Every cadence onto the (already-live) scheduler.** Two cadence
  classes, distinguished explicitly:
  - **Fleet-singleton cadences** (the scheduler's home turf — exactly-once
    per interval via the lease): fold `dream`, `reconcile`, `anki_sync` via a
    **host-affinity** field on `Cadence` (dream stays melchior-pinned for
    OAuth, reconcile caspar-pinned — affinity, not a separate daemon); retire
    their plists and `dream-pass.sh`; correct the stale "ships DARK"
    comments. `catch_up` already fires late-not-lost; an *unpinned* fire
    drops only if the entire fleet is down.
  - **Per-host passes are NOT scheduler cadences.** `heartbeat` (and the
    capability probe it carries) must fire on *every* host — folding it onto
    the exactly-once lease would silence N−1 hosts and gut the very liveness
    signal §H-lifecycle's reclaim depends on. It moves into the one worker's
    loop as a plain per-host pass (its plist still retires, under §L), and it
    must **not depend on the claim machinery it vouches for**.
  - **The affinity carve-out (law 6's one exception):** a pinned cadence
    *does* stall while its pinned host is down — that is the contract, not a
    bug: `catch_up` fires late-not-lost on recovery, and §D alarms on the
    staleness. Law 6's no-stall guarantee applies to unpinned cadences.
- **§L — The collapsed-worker cutover.** Apply the `20b` playbook: one
  flag-free worker per host, behaviour = capability × `prio`. Must be
  **OS-agnostic** (systemd on the Linux nodes, not just launchd — `gr180078`);
  every managed pass restarts on deploy (bounce-coverage gap); teardown reaps
  subprocesses (`gr171254`, `gr176337`); prod DB password out of plists into
  vault (`gr171431`).
  **Cycle a SHIPPED, dark** (`df041901`): `--profile all` = the exact union
  of both rotations (test-pinned down to `_build_handlers`); `20b` rewritten
  to verified env-parity with all four live unit templates per host group;
  `retire-split-agents.yml` authored; worker-agent role split
  provision/units (render-identical for playbook 37).
  **Cycle b EXECUTED 2026-08-04**: canary balthazar → caspar → spark all
  collapsed + verified (spark's split agent unit retired; first prod runs
  of both `service_unit` branches — a launch-time parse bug in its macOS
  reload task was found+fixed, `901a7c26`); `site.yml` +
  `redeploy-precis.yml` now import `20b` in place of `20`+`37`; the
  gateway collapsed via the first flipped deploy. Second same-day fix:
  20b's `roles:`-section `tasks_from:` silently ran the FULL worker-agent
  role (units.yml re-rendered the just-retired split units) — converted
  to task-level `import_role` + the precis_worker role got the same
  provision/units split, both test-pinned. KNOWN RESIDUAL (griped, next
  wave): under `--profile all`, `chase`'s unbounded S2-backoff batches
  can starve same-band reviewers for ~1-2h stretches (85 min observed) —
  the gr187627/gr191264 serial-rotation class, newly exposed by
  co-locating `chase` with the reviewers; fixed (gr192752) by moving
  `structural`/`deep_review` onto scheduler-lease cadences (§A pattern),
  NOT a band reorder (masking). The gateway's
  `precis_agent_container_enabled` stays false pending the
  `dream_agent`-under-`PRECIS_AGENT_CONTAINER` smoke test (static trace
  found no blocker; never empirically exercised there).
  **Blast-radius window — acknowledged.** Today's four profiles crudely
  isolate passes (the 4-day outage killed only the *agent* worker; the
  system worker kept running). One collapsed worker per host means one
  in-process OOM takes down *every* pass on that host — strictly worse,
  on a fleet with OOM history. So the profile merge on a host **trails
  §H's containerization of that host's crash-prone/heavy passes**; the
  flag→`prio` control cutover can land first (it doesn't require merging
  processes). Interim: supervisor KeepAlive + §D staleness cover the gap.
- **§E — Retire the bespoke `app_state` throttles** (paper_reconcile,
  llm_reconcile, backlog_groom, corpus_reconcile, clusterize — each
  re-implements "run every N, single-flight"). Migrate onto the scheduler
  lease once §A is proven. Pure de-duplication; last.

*Folds:* §15i, Track 3, cron-timer retirement, `gr180078`, `gr171254`,
`gr176337`, `gr171431`, `gr172390`, the bounce-coverage gap, watcher-disable
on excluded hosts. *Sub-spec:* `factory-console-and-scheduling.md` §15.

### Pillar 2 — Containerized dispatch + resumable lifecycle (laws 4–6)

- **§H — Containers as the default path for heavy/agentic/GPU work.**
  Generalize the dark `job_claude_docker`/`sandbox_run` pieces into *the*
  execution path; route the `plan_tick`/`fix_gripe` spawn seams through the
  one `call_claude_agent` chokepoint so every dispatch is logged and
  containerized uniformly. Dissolves the **melchior SPOF** (any capable host
  runs agentic containers) and the **co-location jetsam** (the 73 G mlock'd
  weight lives in its own container). Build clean (greenfield license), don't
  patch the seams. Three requirements the traces pin:
  - **Image keyed to the unit, not the host** — one frozen, reproducible image
    per workload (`sandbox_run`'s `image` param), never a kitchen-sink
    (trace 5: the sims' deps genuinely conflict).
  - **`precis_access:read`** — a scoped read-only callback into the corpus, no
    ambient prod creds in the sandbox (the `gr179498` boundary).
  - **Git access without a git-server cardinality** — verify write-back is a
    commit to a `precis-verify/<date>` branch, **pushed on the trusted side**
    to GitHub or a bare repo on the NAS; never push creds inside the sandbox.
    (Gitea would earn a new cardinality only for internal PR/CI/UI.)

  **Isolation substrate:** the three standing nodes are macOS; Linux exists
  only on the DGX Spark tier (rootless podman + GPU passthrough already
  provisioned there). So the **target** is: route sandboxed/heavy work to the
  Sparks — the de-SPOF move and native Linux containers are the same move. On
  the Macs, a container means the colima VM; macOS Seatbelt (`sandbox-exec`)
  is the lightweight native complement for the interim or for a pass pinned
  Mac-native — a complement, not a second substrate (deprecated, macOS-only).
- **§H-lifecycle — the lease is the single job-liveness authority.** Reclaim
  takes over a `running` unit whose lease expired (requeue-from-checkpoint);
  retire the sweeper's `PRECIS_STUCK_JOB_HOURS` wall-clock. Add: a
  **`boot_id`/epoch** on the lease so a bounced worker reclaims its dead
  predecessor's units immediately (`compute-lane-lease-epoch.md`);
  **liveness-aware reclaim** (check the holder's heartbeat, not just
  `lease_until`); a **per-unit attempt cap** distinguishing killed-by-redeploy
  from crash-loop (don't burn the poison guard); a **child-deadlock guard**
  (a parent never blocks forever on a never-completing child).
- **§B-1 — Fix the spark GPU wedge (the one live violation of law 5).**
  `autocatpath_explore` runs the whole NO→NH₃ network × 3 seeds × full NEB as
  one ~90-min un-interruptible in-process CUDA blob — overruns its lease,
  SIGTERM-deaf, takes the worker down (81 starts / 0 completions,
  `gr180096`). The fix is law 5, not a better kill: fan out one
  content-addressed job per `(model, seed)` → `aggregate_partials`; a killed
  seed loses only that seed, a retry skips completed seeds. Stop-gap active:
  `quest:164903` is `STATUS:dormant` — **reverting it to `active` is a
  required step of shipping §B-1.** Build of record: `gpu-priority.md`
  Phase 1 + `autocatpath-integration.md` §3.8.
- **§M — Normalize the work-item ontology.** Audited: the collapse is ~80 %
  done — a todo is one faceted `kind='todo'` (tags + `meta`); ADR 0044 already
  derived the job lane from the parent's kind. The narrow, forward-only
  residue: collapse the `level:` 3-enum into two explicit bits
  (rotation-root?, worker-mintable?); demote `level:recurring` (redundant with
  `meta.schedule`) and `LLM:*` (an auto-close gate) to policy fields; document
  the facet model so the next "type" is a field, not a new tag. No
  LLM-surface aliases needed; only stored refs, web routes, and ~10 nursery
  detectors take the migration.
- **The one boundary that stays: todo ↔ job** — defended on physical grounds
  (ADR 0030): a job is claimed/leased/executor-run (`FOR UPDATE SKIP LOCKED`,
  `idem_key`, sweeper, lease-steal, slots); a todo is durable intent, never
  leased. Merging forces row-lock contention or two state machines on one
  ref. Ruled out explicitly. ("Turn-as-job" is a job-side move, not a merge.)

*Folds:* Tier-B lease-as-liveness, `compute-lane-lease-epoch.md`,
`ssh_node`-deploy-kill, `reclaim_stale_running` liveness gap, morning-brief
child-deadlock, `gr180096`, the sandbox-substrate items, the spawn-seam
containerization, the autocatpath harvest concurrency edge, `gr1821xx`, the
`sim-harness.md` drive path, turn-as-job routing.

### Pillar 3 — Elastic resources on demand (law 1 applied to scarcity)

- **§F — Demand-materialized batches + counted slots + elastic serving.**
  **SHIPPED for the embedder** (`bf7e2581` cycle a: materializer +
  `embed_batch` + `job_inproc` + slot probe; `b2ff8de1` cycle b: cutover —
  materializer default-ON, embed pass manual-only — plus daemon idle-unload
  residency and the env/backend fixes that lit the slot rows; `5e9a5d13`
  agent-role env). **Amendment:** the daemon stays launchd/systemd-supervised;
  residency = idle-unload + lazy-reload IN the daemon
  (`PRECIS_EMBEDDER_IDLE_S`, `/readyz` 200 while idle) — the materializer's
  high-water mint gate already supplies pile-earned spin-up, so no worker
  process management and the watchdog stays. **LLM elastic residency
  (llama-swap models) remains spec** — activation via
  `local-first-capacity-valve.md` first.
  - **Count → threshold → batch-mint.** A cheap periodic demand-count mints a
    batch of low-`prio` jobs when a backlog crosses a threshold (>500
    unembedded chunks → mint the next 5000); below threshold, nothing;
    drained, the claim returns empty. Hysteresis coalesces churn into few
    large batches.
  - **Counted slot at claim** (the LLM half). A job declares
    `requires={'gpu':1}` or `llm:<model>`; the claim decrements the slot in
    the same txn, releases on terminal, crash-reclaimed by the lease sweep.
    Generalize `local_serving`'s acquire/release to any `resource_slots` row;
    seed `llm:` rows from the host's real llama-swap model ids.
  - **Model servers dissolve into this.** The worker starts the server for the
    batch and tears it down when drained: the embedder becomes a slot-bounded
    batch-drainer (cold load amortized across the batch, RAM/GPU freed after)
    — no standing `embedder`/`embedder-watchdog`. "Local deep thinking" is
    the same shape at higher `prio` with an `llm:<local-model>` slot.
  - **Residency is hysteretic** (same high/low-water shape as §C). A cold
    load is seconds for bge-m3 but *minutes* for a big mlock'd model, so
    spin-up is earned by a **pile**, not a trickle: bring the model up above
    a high-water backlog, keep it resident while draining, release below
    low-water — never load/unload per call. And **occasional big-model
    demand doesn't spin up at all**: below the threshold the cheapest move
    is the existing cloud rung for those few calls (same failover path,
    small money); local residency pays only when the pile amortizes the
    load.
  - **Ordering: §B-2's prio-direction pin lands before the first
    materializer.** SATISFIED — the pin shipped first (`de934b16`); the
    materializer mints at `prio=8`, correctly background under `0014` ASC.
- **§B-2 — Priority-claim + reserve mode (kill demoted). SHIPPED**
  (`de934b16` direction pin · `4b2824f5` reserve + kill).
  - **Human-first claim.** The live sort *was* inverted vs `0014` (`DESC` —
    urgent claimed last); reconciled to `ORDER BY COALESCE(r.prio,5) ASC`
    and test-pinned (`test_claim_ordering.py`). `mint_child_job` copies
    parent `prio`.
  - **Reserve mode** — a TTL'd `service_config` row (`service='reserve'`,
    `expires_at`) checked at the top of the heavy claim (ssh_node +
    claude_docker): stop claiming new heavy background; in-flight finishes
    cleanly; auto-expires by predicate. `precis service reserve/release` +
    console banner (§K). The primary responsiveness lever.
  - **Kill backstop — last resort.** `precis jobs kill` stamps
    `kill_requested`; the owning executor kills at its next poll via the
    deadline-kill path, with best-effort `reset_gpu` when the job required
    GPU. Rare by law 5 — the wedge is fixed by chunking (§B-1) and container
    teardown (§H).
- **§C — GPU topology fuse/split — gated, likely shelved.** One Spark's
  ~119 GB already serves ~120 B @ 8-bit / ~200 B @ 4-bit; fusion pays only for
  a frontier model (~400 B+) that cannot quantize onto one unit, and the
  RDMA fabric makes a fused pool a **batch** engine, not interactive. **Run
  the one-Spark-quantized test first; if your models fit, shelve §C** and
  keep `gpu-cluster-modes.md` as the deferred design. Manual `precis cluster
  fuse`/`split` before any autonomy.
- **§I — De-SPOF + co-location relief.** Largely delivered by §H + the
  Sparks; track the ops provisioning explicitly.

*Folds:* slice-6c, `gpu-priority.md`, `gpu-cluster-modes.md`, `gr162694`,
Track 2 (`served_by` seeding), `gr175799`, `gr51393`, spark provisioning
(nvidia runtime, `torch-cuda` base image).

### Pillar 4 — Monitorable (law 6's observability)

- **§D — Liveness net. SHIPPED through Phase 2** (Phase 1 `a1d1573f`:
  Layer-1 checks + derived cadence staleness + derived Layer-2 coherence +
  daily-heartbeat push + dead-man ping; Phase 2 `3424f110`: the remediation
  router — condition-fingerprinted auto-closing gripes after per-class
  self-heal budgets, flood-capped — and the §F coupling made concrete:
  a stale `embed` backlog finding names the first stuck stage of the
  materialize → `embed_batch` → slot-gated chain). SQL-first, composes with
  nursery (the fast critical lane); alarm on backlog-present-but-not-
  draining, never on quiet. Remaining under the §D umbrella: Phase 3 (brief
  lane, surface canaries, alert-triage disposition) and the P6 autonomy
  rungs. Full spec: `health-watchdog.md`.
- **§K — Factory console v2. SHIPPED** (`474b88d0`): per-cadence next-run,
  per-host last-error, `resource_slots` free/capacity chips, reserve
  banner + reserve/release controls, live `prio` knobs (`gr162694`, Track 3
  — only the last-ok/fail click-through drill-down remains).
- **External dead-man's-switch.** An out-of-band `SELECT 1` watcher on a
  different host → Discord — the only signal that survives a total fleet/DB
  outage (the ~8 h prod outage went unalerted because every alerting path was
  DB-backed). Plus set `PRECIS_OPS_ALERT_TARGET` (nursery's critical push is
  dark until it is).

*Folds:* `health-watchdog.md`, `gr162694`, out-of-band DB monitor,
`/checklogs`, config-drift guard, env-gated-pass-absent detection, `gr162141`.

### Pillar 5 — Cost governance & routing (law 3 applied to spend)

- **§G — The dream throttle + the live-knob pattern.** Ship now: keep the
  15-min plist dumb; add `dream.min_interval_minutes` (`app_settings`, no
  migration, default 15 = byte-identical); the pass no-ops if too soon; bump
  to 60 on the budget tab → ~4× fewer dreams (~$46 → ~$11/day), live, no
  redeploy. The pattern (web form → DB knob → self-throttling pass,
  DB>env>default) makes every cadence a live knob; once §A folds dream into
  the scheduler, the knob becomes a `service_config` cadence field.
- **Cost observability + capture.** Per-producer/per-run attribution (join
  `llm_call_log.ref_id` onto job refs) so the knob-turner sees which producer
  to throttle; fix the OpenRouter `cost=null` blindness (`gr171782`) so the
  breaker can meter OpenRouter spend at all.
- **Routing / cheap-tiering.** Mechanical work to small local/cheap models;
  Opus reserved for judgment. The **local-first capacity valve** (run local,
  spill to cloud on saturation with the *same* model — quality-invisible)
  lands here. Consider dropping the `PRECIS_LLM_BACKEND` enum — infer
  transport from the resolved model id.
- **The local flag (must-stay-local / "don't share with big companies").**
  Two rules, simple to state, one hard to build:
  - **Enforcement is default-deny at prompt-assembly time:** if *any* ref in
    the assembled context carries the flag, the call must resolve to a
    local-only chain (or refuse) — never "check at the callsite," since
    context is assembled in one place and callsites are many.
  - **The flag propagates along the derivation graph** — this is the weird
    part: a chunk, summary, embedding-input, card, draft, or dream memory
    *derived from* a flagged ref inherits the flag (a derived artifact's
    level = **max of its inputs**), so sensitivity survives as data forks.
    Every derivation writer must carry it, which is why this is a real build,
    not a tag. Mechanics: `content-sensitivity-placement.md` (it names
    propagation as the genuinely hard problem). Note the valve is unaffected:
    same-model local↔cloud spill must simply *exclude* flagged-context calls
    from the spill path — a flagged call saturates and waits local rather
    than spilling.

*Folds:* `dreamtransfer.md`, Budget guardrails A/C, `gr171782`, `gr175799`,
`local-first-capacity-valve.md`, proprietary-local routing, the
`is_paid(tier)` SMALL-band budget bug.

### Pillar 6 — Guarded autonomy (the auto-fix ladder)

The §D remediation router climbs one rung at a time, each earning trust:
Rung 0 file-gripe → **Rung 1 auto-draft, human-ship** (unattended
reproduce→coder→gate→reviewer→ready-to-`/go` branch; zero autonomous deploy)
→ Rung 2 auto-ship a whitelisted narrow class behind post-deploy verify +
auto-rollback → Rung 3 widen. Safety spine on every rung: reproduce-first
(red test), the `scripts/ship` gate, reviewer sign-off, post-deploy re-check.

- Runs on §H's container substrate — the dependency `health-watchdog.md`
  names.
- **Injection safety (`gr179498`) is a Rung-1 prerequisite:** today's
  `fix_gripe` rail runs `claude -p --dangerously-skip-permissions` on
  verbatim gripe text. Before any rung climbs, the rail must treat
  gripe/finding text as data — sandboxed, no ambient prod credentials.

*Folds:* `health-watchdog.md` §2b, ADR-0048 fixer-loop residuals, `gr179498`,
`sandbox-run-substrate.md`.

## Files & artifacts — git-first

The north-star names Postgres, web, worker, asa — files needed naming too:
sim outputs, harvest artifacts, datasheet PDFs, figure SVGs are bytes that
need a home. **Decided (2026-08-02): git-first; PG indexes it; no hand-rolled
blob store.**

- **Versioned artifacts → git** (bare repos on the NAS). History, blame,
  audit trail (verify's `precis-verify/<date>` commits *are* the provenance
  log), and content-addressing come free — git is itself a CAS. **≤ 1 MB:
  commit directly.**
- **PG holds the searchable index + pointer, not the bytes:** chunks +
  embeddings + `{repo, git_sha, path, content_sha}` — repo *and* commit,
  since a SHA is ambiguous without the repo that owns it. `bytea` is at most
  a hot cache. Law 1 refined: one substrate for state/coordination/index;
  git is the versioned-byte substrate.
- **Large binaries (> 1 MB — plots, VTU/VTI meshes): regenerate, don't
  version.** They're derived from code@`git_sha`, don't diff, and bloat
  history without bound (git keeps a full copy of every version). Default =
  reproduce from the pinned image (trace 5); **git-LFS only for a binary
  genuinely expensive to reproduce** (honestly: LFS is a pointer to an
  object store — the one spot "no bare files" can't fully hold).
- **Why not MinIO/S3 or a hand-rolled CAS:** a new standing system to run;
  git-on-the-NAS already gives content-addressing + history + replication,
  and regeneration covers the rest.

Consequences: sim-harness keeps source/data/findings in the sim's repo and
regenerates plots; §H harvest pushes text back to git on the trusted side;
§D gains one check — NAS git remote reachable + no dangling pointer (a PG
row whose git object is gone).

## Hardware — the incoming 3-Spark cluster

Authoritative inventory: the `git_deploy_helper` ansible repo. Summary:

| Node | OS | Role | Notable |
|---|---|---|---|
| **melchior** | macOS | gateway · agent worker · web · asa | OAuth/`claude_inproc` (today's SPOF); 73 G mlock'd weight co-locates here |
| **spark** (DGX, 1 today → 4) | Linux | inference · agent worker · GPU | cores 0–1 fenced for the system; rootless podman + GPU passthrough provisioned — §H's substrate is proven here |
| **caspar** | macOS | Postgres · NFS · redis | the run-once data node |
| **balthazar** | macOS | small Mac | SMALL-tier local model host |

Native Linux containers exist **only on the Sparks** — which is why they are
the linchpin for Pillar 2 (isolation + de-SPOF are the same routing move).
The spark core-fence (system on 0–1, compute on 2–19) is law 5 in the OS.

The +3 Sparks are why Pillar 3 is central, not later: more `gpu` slots, more
agent-capable hosts, more container capacity — **disaggregated by default**
(N independent per-Spark slots, zero cross-node traffic), fused only per §C's
gate. Provisioning debt to clear first: nvidia docker runtime not in ansible,
no `torch-cuda` base-image mirror, and §L's OS-agnostic manage story
(`gr180078` — the Sparks are systemd).

## Worked lifecycles — what happens when you run X

> Six end-to-end traces showing how the laws compose. Status markers:
> **[live]** wired in prod · **[dark]** built, gates itself off ·
> **[spec]** design only. The moving-pieces index is the per-mechanism
> status authority.

### Moving-pieces index

| Piece | What it does | Status | Durable anchor |
|---|---|---|---|
| **llama-swap** | per-host GGUF server, one `llama-server`/model, `--parallel N` slots | **live** where GPU present | `deploy/roles/llamacpp` |
| **`serve-embeddings`** (bge-m3) | remote embedder HTTP service | **live** (ADR 0020) | `cli/serve_embeddings.py` · `com.precis.embedder` |
| **`served_by` advertiser** | heartbeat writes this host's served models into the `llm` card | **dark** (empty until cutover) | `workers/llm_serving.py::advertise_local_llm` |
| **`resource_slots` — hardware** | counted `gpu`/`podman`/`tts`, reserve-at-claim | **live**, self-gating | `store/_resource_slots_ops.py` · `executors/_common.py::claim_executor_jobs` |
| **`resource_slots` — `llm:<model>`** | counted local-serve slots for LLM calls | **dark** (no-op until `served_by`) | `utils/llm/local_serving.py::acquire`/`release` |
| **`resolve_chain` / rungs** | tier → ordered `local`/`cloud` rungs | **live**; local rungs latent | `utils/llm/router.py::resolve_chain` |
| **failover-at-load** | all-N-slots-busy → spill the same request to the hosted endpoint | **live** | `router.py::dispatch` |
| **`select_offering`** | Pareto pick over the catalog (local cards price at 0) | **live** | `utils/llm/policy.py::select_offering` |
| **`service_config.prio`** | live per-host on/off + concurrency, 5 s TTL | **live** | `workers/service_config.py` · `cli/service.py` |
| **capability probe** | heartbeat probes gpu/podman/tts/embedder → UPSERT slot rows | **live** | `workers/capability_probe.py::probe_host_resources` |
| **scheduler pass** | lease-backed recurring clock | **live in prod** | `workers/scheduler.py` · `Store.claim_scheduler_lease` |
| **executor claim** | `FOR UPDATE SKIP LOCKED`, `ORDER BY COALESCE(prio,5) ASC` (0014 direction, test-pinned §B-2), reserve-at-claim | **live** | `executors/_common.py::claim_executor_jobs` |
| **reserve mode** | TTL'd `service='reserve'` row gates new heavy claims (ssh_node + claude_docker); auto-expires | **live** (§B-2) | `workers/service_config.py::reserve_active` · `cli/service.py` |
| **operator kill** | `precis jobs kill` stamps `kill_requested`; owning executor kills at next poll + GPU reset | **live** (§B-2) | `cli/jobs_admin.py` · `executors/_common.py::maybe_reset_gpu_after_kill` |
| **materializer + `embed_batch`** | backlog-count → mint slot-gated bounded batch jobs; `job_inproc` executor drains | **live** (default-ON; `PRECIS_MATERIALIZE_EMBED=0` opt-out) | `workers/materialize.py` · `workers/job_types/embed_batch.py` · `executors/job_inproc.py` |
| **embedder idle-unload** | daemon releases model RAM/GPU after idle, lazy-reloads on demand | **live** (`PRECIS_EMBEDDER_IDLE_S`, 1800s) | `embedder_service.py` · ADR 0020 |
| **`mint_child_job`** | mints child jobs, copies parent `prio` | **live** | `workers/dispatch.py::run_dispatch_pass` |
| **container executors** | dispatch heavy/agentic work into rootless podman; `mode:run` + pinned `image` + `precis_access:read` | **dark**/gated | `executors/{claude_docker,agent_container}.py` · `job_types/sandbox_run.py` |
| **§C fuse/split** | RDMA topology modes | **spec** (gated) | `gpu-cluster-modes.md` |
| **§F elastic serving** | spin a model up for a batch, release when drained | **live for the embedder** (rows above); **spec for LLM** models | this doc, §F |

### The serving primitive (shared by all LLM traces)

1. A GPU host stands up **llama-swap**, one `llama-server` per catalog model,
   `--parallel N` slots. **[live where GPU]**
2. The heartbeat **advertises** served models into the `llm` card's
   `meta.served_by`, reconciled into counted `llm:<model>` slot rows.
   **[dark — no card carries `served_by` yet, so every model no-ops to cloud
   today. Activation vehicle: `local-first-capacity-valve.md` seeds
   `served_by` + `llm:` slots for SMALL first — gated on its B1/B2 (pick M,
   measure N)]**
3. A call resolves tier → **rungs** (`resolve_chain`). A local rung reserves
   an `llm:<model>` slot; a free slot rewrites the call to llama-swap;
   **all-N-busy spills the same request to the hosted endpoint**
   (quality-invisible). **[live plumbing; latent while 2 is dark]**
4. **§F makes it elastic:** the worker starts the server when backlog + a
   free slot call for it, tears it down when drained — warm for the batch,
   RAM/GPU freed after. **[spec for LLM servers — today they stand as
   daemons. Shipped for the embedder in the amended shape: the daemon
   stands, the MODEL unloads after idle (`b2ff8de1`)]**

### 1. "I need a small model" (classify / summarize / triage)

Fits one node; never leaves disaggregated mode.

1. The pass runs where `service_config.prio > 0` (live on/off). **[live]**
2. Tier SMALL → `resolve_chain` reads `llm.chain.small` — today a cloud rung
   (OpenRouter glm-4.7-flash); target: local rung first, cloud second.
   **[live]**
3. `local_serving.acquire` reserves a slot **[dark until `served_by`]**; free
   slot → §F spins the server up if cold, rewrites to llama-swap, runs,
   releases; all-busy → spill to hosted, no failure, no quality change.
4. One node's slots drain many small calls in parallel. Never fuses.

### 2. "I want the big guy" (frontier / deep reasoning)

Cheapest-fit first; whether you *ever* fuse hinges on one measurable
question — does the model fit one Spark quantized?

- **(a) Fits one Spark** (~120 B @ 8-bit / ~200 B @ 4-bit on ~119 GB) —
  disaggregated, low `--parallel`; §F brings it up for the pile of
  big-thinking work, drains, releases. A *lone* big call spins nothing up —
  below §F's high-water it just rides the cloud rung (c) for small money;
  local residency is earned by a pile. **If this test passes, §C is
  shelved.** **[spec serving; the test gates §C]**
- **(b) Too big to quantize onto one unit** (~400 B+) — §C fuses N Sparks
  into one `bigpool` over RDMA. **Batch, not interactive** (RDMA ≠ NVLink);
  big jobs queue and run as a batch. Hysteretic — see trace 6. **[spec]**
- **(c) Cloud (Opus/Sonnet)** — today's reality and the standing failover;
  under `llm.cloud_enabled=false` calls pause (skip, not fail). **[live]**
  `dream` dominates this spend — §G throttles it.

### 3. "I run a simulation" (DFT `struct_relax` — a GPU executor job)

Not an LLM call — a counted GPU job; the cleanest reserve-at-claim example.
(External-sim repos are trace 5.)

1. A parent todo mints `job_type=struct_relax`, `requires={gpu:1}`, parent
   `prio` copied. **[live]**
2. A worker on a GPU-advertising host claims it; **the same txn reserves the
   `gpu:1` slot** (decrement = lock, self-gating). **[live —
   `test_reserve_at_claim.py`]**
3. Runs the relaxation; §H target: in a dispatched container with
   `kill_container` + `reset_gpu` teardown. **[partial]**
4. Terminal refunds the slot; crash → lease expires → reclaim steals the row,
   slot reclaimed. **[live]**
5. Many independent GPU jobs drain across per-node slots — disaggregated;
   +3 Sparks ≈ 4× parallel slots. No fusion.

### 4. "I run autocatpath" (NO→NH₃ pathway explore — §B-1)

The poster child for resumable-not-killable.

- **Today [live bug]:** one ~90-min un-interruptible CUDA blob; overruns its
  lease, SIGTERM-deaf, SIGKILL'd, takes the worker down (81 starts / 0
  completions, `gr180096`). Stop-gap: `quest:164903` dormant.
- **Target [spec]:** fan out one content-addressed job per `(model, seed)`
  (`run_one_seed`) + `aggregate_partials`:
  1. Each seed a small `requires={gpu:1}` unit, claimed + slot-reserved as in
     trace 3, run in its own container; a retry **skips completed seeds**.
  2. A killed seed loses only that seed; the worker stays SIGTERM-responsive.
  3. `aggregate_partials` waits on `child_job_succeeded` for all seeds → the
     same scalar barrier harvested today.
  4. Seeds spread across per-node `gpu` slots — disaggregated; no fusion.
  - Shipping §B-1 **requires reverting `quest:164903` to `STATUS:active`.**

### 5. "I run an external simulation" (sim-harness drive path)

precis drives a standalone Pareto-sim repo (`flyinghose`, `flowsim`,
`lighterthanair`): verify its data against the literature, run it, ingest the
outputs (`sim-harness.md`). The workload that most needs §H's container, and
the source of §H's image-pinning + `precis_access:read` requirements.

1. **[live — slice 1]** `precis sim verify/ingest <slug>` as a plain CLI
   verb — no container, no worker; ships independently of this plan.
2. **[spec — the container path]** A job mints `sandbox_run` `mode:run` with
   the sim's pinned `image` + `precis_access:read`; the worker claims it
   (reserve-at-claim as in trace 3), dispatches the container; the sim runs,
   calls precis read-only for in-run context; outputs harvest back into the
   corpus (text → git, trusted-side push); container torn down, worker
   lives. Exactly §H.
3. **[spec — slice 2]** A `level:recurring` watch under the sim's quest
   re-runs verify/ingest/writeup as literature or the sim drifts — a §A
   cadence + §F demand shape, not a bespoke loop.

Each sim is an independent per-node container unit; no fusion.

### 6. How it's clustered — disaggregated by default, fused only for a batch

The workload is bursty and heterogeneous across many parallel projects — a
pile of little things, then a pile of thinking, then little things again.
Two **orthogonal, hysteretic** knobs manage it with no fairness scheduler:

- **§F — how much work to materialize.** Each backlog batch-mints above its
  threshold and mints nothing when drained; "little things" and "piles of
  thinking" accumulate and drain on independent clocks.
- **§C — what shape the silicon is in.** Disaggregated covers traces 1, 3,
  4, 5, and 2(a): independent per-Spark slots, zero cross-node traffic.
  Fusion is the exception, paid only for 2(b).

Why the tie-together cost stays rare: the Sparks are physically tied by the
RDMA fabric at all times but **logically fused rarely**. Fusion is pull-based
and hysteretic — a pile of big-model demand crossing a **high-water** mark
pulls a fuse; the pool serves the pile as a batch; it splits only after
demand stays below a **low-water** mark (the gap prevents thrash). You never
fuse for a single interactive request — you fuse for an accumulated pile, so
the overhead is amortized across the batch. One user ⇒ the dominant pile
pulls the topology; start with manual `precis cluster fuse`/`split`, earn
autonomy only if manual proves tedious.

## Ship order

1. **Standalone wins, now:** §B-1 (correctness; + revert `quest:164903`) and
   §G's dream throttle (cost; no redeploy).
2. **P1:** §A fold the three fleet-singleton cadences (heartbeat moves into
   the worker loop per-host, not onto the lease) + fix stale comments; §L's
   **control cutover** (env-flags → `prio`, launchd + systemd) — but the
   **profile merge on a host trails §H** for hosts running crash-prone
   in-process passes (the blast-radius window).
3. **P2:** §H containerized dispatch + §H-lifecycle (lease-as-liveness,
   epoch, liveness-aware reclaim); then finish §L's profile merge. §M can
   land small and early, before §H hardens shapes.
4. **P3:** §B-2's prio-direction pin **first** (it defines what "low-prio"
   means), then §F (LLM slots + materializer + elastic serving) alongside
   the rest of §B-2; §C only if the quantized test fails.
5. **P4:** §D Layer 1 anytime; §K console; dead-man's-switch; §D Layer 2
   with/after §F (shared backlog signal).
6. **P5** beyond §G; **P6** last (needs §H + `gr179498`).
7. **§E** only after §A is proven live ≥ 1 week.

## Acceptance criteria

- **§B-1:** seed-per-job + aggregate tree; a killed seed loses only that
  seed; a retry skips completed seeds; the worker stays SIGTERM-responsive;
  the aggregate yields the same scalar barrier as today.
- **§A:** each folded fleet-singleton cadence fires exactly once per
  interval fleet-wide — no double-fire during overlap; an *unpinned* cadence
  drops no fire when the previously-owning host is down; a *pinned* cadence
  stalls while its host is down, `catch_up` fires late-not-lost on recovery,
  and §D flags the staleness; `heartbeat` keeps firing per-host on every
  live host (it is not on the lease).
- **§L:** one flag-free worker per host behaves identically to today's
  matrix; a console `prio` change takes effect within one claim cycle, no
  redeploy; manage/restart works on launchd **and** systemd; every managed
  pass restarts on deploy; no cleartext DB password in any plist.
- **§H:** a heavy/agentic unit runs in a dispatched container; teardown
  leaves the worker alive and the unit re-claimable; an agentic unit runs on
  a non-melchior host; the mlock'd weight is not co-resident with the worker.
- **§H-lifecycle:** a bounced worker reclaims its dead predecessor's units on
  the first claim pass; a live holder is never stolen; a redeploy mid-run
  does not burn the poison guard; a parent never blocks forever on a child.
- **§B-2:** a human-urgent unit is claimed ahead of background, direction
  reconciled to `0014` and pinned by a test; reserve mode stops new heavy
  dispatch within one claim cycle and auto-expires; force-kill exercised only
  in the injected-hang drill and reclaims the GPU.
- **§F:** a backlog above threshold mints a bounded batch and no more until
  it drains; `requires={'gpu':1}` cannot be claimed at `free=0` (injected
  two-claim race leaves one queued); slots release on terminal and reclaim on
  crash; embeddings drain as batched slot-gated jobs; the model is warm for a
  batch and released after.
- **§C:** fuse drains to low-water, stands the pool up; split tears down with
  no orphaned reservation; modes mutually exclusive; a dead pool node
  releases the pool and requeues its jobs.
- **§D:** a stopped cadence shows stale within interval+margin; a
  correctly-idle producer does not alarm while a non-draining backlog does
  (same signal); the digest still sends templated when the LLM/fleet is down;
  the dead-man's-switch fires on a total outage.
- **§G:** `dream.min_interval_minutes=60` no-ops in-interval passes within
  one cadence, no redeploy; DB>env>default (unset = byte-identical);
  per-producer cost attributes `claude_agent` spend by source; OpenRouter
  logs non-null cost.
- **§K:** the console shows per-host next-run + last-error + slot
  free/capacity; a `prio` set there changes scheduling within one claim
  cycle.
- **§M:** `level:` 3-enum → two explicit fields; `level:recurring`/`LLM:*` →
  policy fields; forward-only migration, no LLM-surface alias; a test pins
  that a job still leases and a todo never does.
- **§E:** each migrated throttle fires on the same cadence, single-flight
  preserved, interval declared once; no behaviour change beyond tick source.

## Explicitly NOT in scope

- Multi-class fairness scheduling (fair-share, gang, bin-packing, mid-run
  yield) — moot with one user.
- A dispatcher / singleton scheduler daemon — the claim substrate needs none.
- Interactive serving from a fused pool — the interconnect makes it batch.
- Routine force-kill as a responsiveness lever — reserve+drain and container
  teardown are primary.

## Decisions log & open questions

Decided (all 2026-08-02, Reto, unless noted):

- **One unified master doc** (2026-08-01); the sub-specs stay mechanical.
- **North-star = Postgres + web + worker + asa**; three singletons + per-host
  worker; container runtime + infra are sidecars.
- **One scheduler folds every cadence** — dream/reconcile/anki included, via
  host-affinity; pinning is an affinity, not a daemon.
- **Resumable, not killable** — small idempotent units; kill containers;
  force-kill last-resort.
- **Containerized dispatch in scope** (§H), greenfield-clean at the seams.
- **Model-servers worker-spun on demand** (§F) — retires the embedder +
  watchdog plists.
- **Isolation substrate** — target rootless podman on the Linux Sparks;
  Seatbelt/colima are the all-Mac interim complement.
- **Git access** — push on the trusted side to GitHub or a NAS bare repo; no
  git-server cardinality.
- **File/blob storage — git-first** (≤ 1 MB commit; > 1 MB regenerate or
  LFS); PG indexes + `{repo, git_sha, path, content_sha}`; `bytea` = cache.
- **Todo↔job boundary stays** (ADR 0030 physical grounds) — ratify, don't
  drift into a merge.
- **§D↔§F shared liveness** — one registry + one backlog signal (also in
  `health-watchdog.md`).
- **Scheduler-flag drift resolved** — the pass is live; §A extends, not
  flips.

Open:

- **§A** — host-affinity representation: per-`Cadence` field vs a `prio` cell
  on the lease. Pin before building.
- **§H** — which passes containerize first (agentic + GPU are the wins);
  reuse the `job_claude_docker` seam or rebuild clean?
- **§H** — is any agentic/untrusted work pinned Mac-native long-term (⇒
  invest in Seatbelt) or does everything route to the Sparks (⇒ interim
  only)?
- **§F** — materializer placement: one generic pass reading
  `(count-query, threshold, batch, resource)` from each `ServiceSpec`, vs
  per-producer minting.
- **§F** — reshape vs leave-standing per producer (`embed` already drains
  correctly; clear wins are slot-gating GPU/local-model producers + elastic
  residency).
- **§C** — needed at all? Gate on the one-Spark-quantized test.
- **§G** — knob shape (cadence recommended vs daily cap vs on/off); bundle
  the cost view or ship the knob first.
- **Files** — is the `folder` kind the blob-set container; the
  LFS-vs-regenerate line per artifact class.
- **Local flag** — taint-propagation completeness: enumerate every
  derivation writer (ingest → chunks → embeddings → summaries → cards →
  drafts → dream memories) and verify each carries the flag; and where does
  the max-of-inputs computation live so a new writer can't silently skip
  it? (`content-sensitivity-placement.md`.)
- Housekeeping (not this plan): `gr162141`, `gr55762` self-described shipped
  but still open — verify + close.

**ADR-0048 readiness pass (2026-08-01):** resolved into the body. The one
blocker — §B-2's claim-order target was wrong; the live sort may already be
inverted vs `0014` — is now §B-2's reconciliation framing. Full findings in
git history of this file.

## Relationship to the sub-specs

This doc is the index + ordering + north-star; it duplicates no mechanics.
Precedence: the sub-spec wins on mechanics, this doc on cross-axis ordering
and the laws.

**Live sub-specs** (mechanical detail this doc doesn't carry):

- `gpu-priority.md` → §B — Phase 1 is the §B-1 build of record (with
  `autocatpath-integration.md` §3.8). Status: shipped (§B-1 chunking +
  §B-2 pin/reserve/kill); kept as the design record.
- `health-watchdog.md` → §D + Pillar 6's ladder (§2b).
- `compute-lane-lease-epoch.md` → §H-lifecycle (the epoch).
- `sim-harness.md` → trace 5 / §H's image-pinning + `precis_access:read`
  requirements (slice 1 is its own application, independent of this plan).
- `sandbox-run-substrate.md` → §H (status: built, dark; slices 2–3 = §H).
- `content-sensitivity-placement.md` → Pillar 5's must-stay-local guard —
  the one genuinely unbuilt piece of the routing cluster.
- `local-first-capacity-valve.md` → Pillar 5 activation — **the first
  activation of the serving primitive's two dark pieces** (`served_by` +
  `llm:` slots), for SMALL; gated on two blockers (pick model M, measure
  real `max_parallel` N). Deliberately kept separate: it's shovel-ready and
  ships through the proposal pipeline as a unit. Interim: it holds M *hot*
  as a standing llama-swap; §F later folds residency into elastic
  spin-up/down on the same substrate.
- `factory-console-and-scheduling.md` (docs/design/) → §K + the
  console/registry/capability detail; its §15 scheduling framing is
  superseded by this doc.

**Parked / retired** (triage 2026-08-02):

- `gpu-cluster-modes.md` → §C — parked behind the one-Spark-quantized test;
  the disaggregated half is simply this doc's default.
- `glm-fleet-flip-safety.md` — **deleted** (landed 2026-07-25; durable
  record = ADR 0066 + git history).
- `llm-openrouter-bypass.md` + `llm-operation-routing.md` — largely shipped
  (ADR 0066 is the record); headers stamped, deletion candidates once their
  minor residuals are filed.

Full wedge trail: `gr180096`.

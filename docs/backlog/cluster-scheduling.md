---
status: draft
title: Cluster consolidation — one worker, one scheduler, one substrate; containerized, monitorable, elastic
model: opus
---

# Cluster consolidation (unified master plan)

> **The one plan to review.** Subsumes the scheduling framing previously
> scattered across `factory-console-and-scheduling.md` §15, `gpu-priority.md`,
> `gpu-cluster-modes.md`, `health-watchdog.md`, and the related residual
> items. Those remain the mechanical sub-specs (reconciliation at the end);
> this doc is the frame, the target state, the current state, and the
> ordering. Greenfield where legacy is messy — tied only to what is already
> proven live.

## North star

**Three cluster-wide singletons** — Postgres (caspar), web (gateway), asa
(chat bridge) — **plus one thin worker per host.** Nothing else is a
standing precis daemon: dream / cron-tick / watch-poll / anki-sync /
reconcile / embedder-watchdog all collapse into the worker's loop or a
dispatched container; model servers are worker-spun on demand and torn
down when the backlog drains. The `PRECIS_*_ENABLED` flags and the
plist-per-daemon model retire. Liveness is one outcome-based digest
(alarm on backlog-present-but-not-draining, never on quiet); cost is a
live per-producer knob; versioned artifacts live in git, Postgres
indexes them.

## The seven design laws (the spine)

A proposal that violates one is wrong.

1. **One substrate.** The decentralized derived claim queue (ADR 0007/0017);
   reserve-at-claim; every scheduler concern is a policy on the claim.
2. **One scheduler.** The lease-backed recurring clock folds every cadence.
3. **One control surface.** A pass runs on a host iff capability ×
   `service_config.prio` says so — live from the web.
4. **The worker is thin; work runs in dispatched containers.** Carve-outs:
   the three singletons; worker-spun model-servers (§F).
5. **Resumable, not killable.** Small idempotent/content-addressed units;
   kill the container, never the worker; force-kill is a rare escape hatch.
6. **Correctness in Postgres, never in a host.** A host being down must never
   drop a fire, wedge a unit, or stall a cadence — one exception: an
   **affinity-pinned** cadence stalls while its pinned host is down, by
   design (§A carve-out).
7. **One user ⇒ no fairness scheduler.**

**Failure vocabulary:** a **fire** is one scheduled occurrence (*dropping* =
never runs; `catch_up` is late-not-lost); a **unit** is one claimed, leased
`kind='job'` row (*wedging* = stuck non-terminal, holding lease + slot); a
**cadence** is a recurring schedule (*stalling* = stops emitting future
fires).

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

## Current state (2026-08-04; details in executor/worker docstrings)

Live and load-bearing: the claim substrate (all four executors,
capability-reserved claim, `mint_child_job` prio copy), the hardware half of
`resource_slots` (0073; reserve-at-claim + refund, self-gating), the
`scheduler` pass (`cron_tick` + `watch_poll` folded, timers retired), §B-2
(prio-direction pin, reserve mode, `precis jobs kill`), §D through Phase 2,
§K console v2, §F for the embedder (materializer + `embed_batch` +
idle-unload), and the §L collapsed-worker cutover (cycles a+b executed;
split units retired).

Dark / spec: the **LLM half of `resource_slots`** (`llm:<model>` slots no-op
until a card carries `served_by` — activation vehicle:
`local-first-capacity-valve.md`); **containerized dispatch**
(`job_claude_docker`, colima sidecar, `sandbox_run` `mode:build`, and the
`plan_tick`/`fix_gripe` spawn seams that bypass `call_claude_agent`); §F
elastic residency for LLM models; §C fuse/split.

Cost is dominated by one producer: `dream` (~79 % of cluster LLM spend,
~$46/day) on a hardcoded 15-min timer — §G is the near-term fix.

## Pillar 1 — one worker, one scheduler, one control surface (laws 1–3)

- **§A — remaining cadences onto the live scheduler.** Fold the three
  fleet-singleton cadences — `dream` (gateway, 15-min), `reconcile` (caspar,
  daily), `anki_sync` (30-min) — via a **host-affinity** field on `Cadence`
  (affinity, not a separate daemon); retire their plists and `dream-pass.sh`;
  correct the stale "ships DARK" comments. **Per-host passes are NOT
  scheduler cadences:** `heartbeat` (+ its capability probe) must fire on
  every host — it moves into the worker loop as a plain per-host pass and
  must not depend on the claim machinery it vouches for.
- **§L residuals** (cutover itself shipped): the gr187627/gr191264
  serial-rotation class — `chase` starving same-band reviewers under
  `--profile all`; gr192752's fix = `structural`/`deep_review` onto
  scheduler-lease cadences (§A pattern, not a band reorder). The gateway's
  `precis_agent_container_enabled` stays false pending the
  `dream_agent`-under-`PRECIS_AGENT_CONTAINER` smoke test.
  **Blast-radius constraint:** the per-host profile merge trails §H's
  containerization of that host's crash-prone passes (one in-process OOM
  must not take down every pass on a fleet with OOM history).
- **§E — retire the bespoke `app_state` throttles** (paper_reconcile,
  llm_reconcile, backlog_groom, corpus_reconcile, clusterize) onto the
  scheduler lease once §A is proven. Pure de-duplication; last.

## Pillar 2 — containerized dispatch + resumable lifecycle (laws 4–6)

- **§H — containers as the default path for heavy/agentic/GPU work.**
  Generalize the dark pieces into *the* execution path; route the
  `plan_tick`/`fix_gripe` spawn seams through the one `call_claude_agent`
  chokepoint. Dissolves the melchior SPOF and the co-location jetsam (the
  73 G mlock'd weight gets its own container). Requirements the traces pin:
  image keyed to the unit (never kitchen-sink); `precis_access:read` (no
  ambient prod creds in a sandbox — the `gr179498` boundary); git write-back
  pushed on the trusted side (no creds inside the sandbox, no new git-server
  cardinality). Substrate: rootless podman on the Linux Sparks is the
  target; Seatbelt/colima are the Mac interim complement, not a second
  substrate.
- **§H-lifecycle — the lease is the single job-liveness authority.** Reclaim
  takes over a `running` unit whose lease expired; retire the sweeper's
  `PRECIS_STUCK_JOB_HOURS` wall-clock. Add: a `boot_id`/epoch on the lease
  (bounced worker reclaims its dead predecessor's units immediately —
  `compute-lane-lease-epoch.md`); liveness-aware reclaim (check the holder's
  heartbeat); a per-unit attempt cap (killed-by-redeploy ≠ crash-loop); a
  child-deadlock guard.
- **§B-1 — fix the spark GPU wedge (the one live violation of law 5).**
  `autocatpath_explore` runs ~90 min un-interruptible in-process CUDA;
  overruns its lease, SIGTERM-deaf, takes the worker down (81 starts /
  0 completions, `gr180096`). Fix = law 5: fan out one content-addressed job
  per `(model, seed)` → `aggregate_partials`; a killed seed loses only that
  seed; a retry skips completed seeds. Build of record: `gpu-priority.md`
  Phase 1 + `autocatpath-integration.md` §3.8. **Shipping §B-1 requires
  reverting `quest:164903` to `STATUS:active`** (dormant stop-gap).
- **§M — normalize the work-item ontology** (~80 % done already). The
  narrow, forward-only residue: collapse the `level:` 3-enum into two
  explicit bits (rotation-root?, worker-mintable?); demote `level:recurring`
  (redundant with `meta.schedule`) and `LLM:*` to policy fields; document the
  facet model. Only stored refs, web routes, and ~10 nursery detectors take
  the migration.
- **The one boundary that stays: todo ↔ job** (ADR 0030, physical grounds) —
  a job is claimed/leased/executor-run; a todo is durable intent, never
  leased. Merging is ruled out explicitly.

## Pillar 3 — elastic resources on demand (law 1 applied to scarcity)

- **§F — demand-materialized batches + counted slots + elastic serving.**
  Shipped for the embedder (amended: the daemon stays supervised; residency
  = idle-unload + lazy-reload in the daemon). **LLM elastic residency
  remains spec** — activation via `local-first-capacity-valve.md` first.
  Open mechanics: generalize `local_serving`'s acquire/release to any
  `resource_slots` row; seed `llm:` rows from the host's real llama-swap
  model ids; residency is hysteretic (spin-up earned by a pile crossing
  high-water, released below low-water — never load/unload per call; a lone
  big call rides the cloud rung instead).
- **§C — GPU topology fuse/split — gated, likely shelved.** One Spark's
  ~119 GB already serves ~120 B @ 8-bit / ~200 B @ 4-bit; fusion pays only
  for a frontier model that cannot quantize onto one unit, and RDMA makes a
  fused pool a batch engine, not interactive. **Run the one-Spark-quantized
  test first; if the models fit, shelve §C** (`gpu-cluster-modes.md` stays
  the deferred design). Manual `precis cluster fuse`/`split` before any
  autonomy; fusion is pull-based and hysteretic, never for a single
  interactive request.
- **§I — de-SPOF + co-location relief.** Largely delivered by §H + the
  Sparks; track the ops provisioning explicitly (nvidia docker runtime in
  ansible, `torch-cuda` base-image mirror, systemd manage story
  `gr180078`).

## Pillar 4 — monitorable (law 6's observability)

- **§D** — shipped through Phase 2. Remaining: Phase 3 (brief lane, surface
  canaries, alert-triage disposition) + the P6 autonomy rungs. Full spec:
  `health-watchdog.md`.
- **§K** — console v2 shipped; only the last-ok/fail click-through
  drill-down remains.
- **External dead-man's-switch.** An out-of-band `SELECT 1` watcher on a
  different host → Discord — the only signal that survives a total fleet/DB
  outage (the ~8 h prod outage went unalerted because every alerting path
  was DB-backed). Plus set `PRECIS_OPS_ALERT_TARGET` (nursery's critical
  push is dark until it is).

## Pillar 5 — cost governance & routing (law 3 applied to spend)

- **§G — the dream throttle + the live-knob pattern.** Ship now:
  `dream.min_interval_minutes` in `app_settings` (default 15 =
  byte-identical); the pass no-ops if too soon; bump to 60 on the budget tab
  → ~4× fewer dreams, live, no redeploy. Once §A folds dream into the
  scheduler, the knob becomes a `service_config` cadence field.
- **Cost observability + capture.** Per-producer/per-run attribution (join
  `llm_call_log.ref_id` onto job refs); fix the OpenRouter `cost=null`
  blindness (`gr171782`).
- **Routing / cheap-tiering.** Mechanical work to small local/cheap models;
  Opus reserved for judgment; the local-first capacity valve lands here.
  Consider dropping the `PRECIS_LLM_BACKEND` enum — infer transport from the
  resolved model id.
- **The local flag (must-stay-local).** Two rules: enforcement is
  default-deny at prompt-assembly time (never per-callsite); the flag
  propagates along the derivation graph (a derived artifact's level = max of
  its inputs) — every derivation writer must carry it, which is why this is
  a real build, not a tag. Mechanics: `content-sensitivity-placement.md`.
  The valve must exclude flagged-context calls from the cloud-spill path.

## Pillar 6 — guarded autonomy (the auto-fix ladder)

The §D remediation router climbs one rung at a time: Rung 0 file-gripe →
Rung 1 auto-draft, human-ship → Rung 2 auto-ship a whitelisted narrow class
behind post-deploy verify + auto-rollback → Rung 3 widen. Safety spine on
every rung: reproduce-first (red test), the `scripts/ship` gate, reviewer
sign-off, post-deploy re-check. Runs on §H's substrate. **Injection safety
(`gr179498`) is a Rung-1 prerequisite:** the `fix_gripe` rail must treat
gripe/finding text as data — sandboxed, no ambient prod credentials.

## Files & artifacts — git-first (decided 2026-08-02)

Versioned artifacts → git (bare repos on the NAS; ≤ 1 MB commit directly).
PG holds the searchable index + pointer (`{repo, git_sha, path,
content_sha}`), never the bytes (`bytea` at most a hot cache). Large
binaries (> 1 MB) regenerate from code@`git_sha`; git-LFS only for a binary
genuinely expensive to reproduce. No MinIO/S3/hand-rolled CAS — git already
gives content-addressing + history + replication. §D gains one check: NAS
git remote reachable + no dangling pointer.

## Hardware — the incoming 3-Spark cluster

melchior (macOS gateway; OAuth SPOF; 73 G mlock'd weight), spark (DGX Linux,
1 → 4; rootless podman + GPU passthrough proven; cores 0–1 fenced), caspar
(Postgres/NFS/redis), balthazar (SMALL-tier Mac). Native Linux containers
exist only on the Sparks — isolation + de-SPOF are the same routing move.
+3 Sparks are why Pillar 3 is central: disaggregated by default, fused only
per §C's gate.

## Ship order

1. **Standalone wins, now:** §B-1 (correctness; + revert `quest:164903`) and
   §G's dream throttle (cost; no redeploy).
2. **P1:** §A fold the three fleet-singleton cadences; §L residuals — the
   profile merge on a host trails §H for crash-prone in-process passes.
3. **P2:** §H containerized dispatch + §H-lifecycle; then finish §L's
   profile merge. §M can land small and early, before §H hardens shapes.
4. **P3:** §F (LLM slots + materializer + elastic serving); §C only if the
   quantized test fails.
5. **P4:** §D Phase 3; dead-man's-switch; §K drill-down.
6. **P5** beyond §G; **P6** last (needs §H + `gr179498`).
7. **§E** only after §A is proven live ≥ 1 week.

## Acceptance criteria

- **§B-1:** seed-per-job + aggregate tree; a killed seed loses only that
  seed; a retry skips completed seeds; the worker stays SIGTERM-responsive;
  the aggregate yields the same scalar barrier as today.
- **§A:** each folded fleet-singleton cadence fires exactly once per
  interval fleet-wide; an unpinned cadence drops no fire when the
  previously-owning host is down; a pinned cadence stalls while its host is
  down, `catch_up` fires late-not-lost, §D flags the staleness; `heartbeat`
  keeps firing per-host (not on the lease).
- **§H:** a heavy/agentic unit runs in a dispatched container; teardown
  leaves the worker alive and the unit re-claimable; an agentic unit runs on
  a non-melchior host; the mlock'd weight is not co-resident with the
  worker.
- **§H-lifecycle:** a bounced worker reclaims its dead predecessor's units
  on the first claim pass; a live holder is never stolen; a redeploy mid-run
  does not burn the poison guard; a parent never blocks forever on a child.
- **§F:** a backlog above threshold mints a bounded batch and no more until
  it drains; `requires={'gpu':1}` cannot be claimed at `free=0`; slots
  release on terminal and reclaim on crash; the model is warm for a batch
  and released after.
- **§C:** fuse drains to low-water, stands the pool up; split tears down
  with no orphaned reservation; a dead pool node releases the pool and
  requeues its jobs.
- **§D:** a stopped cadence shows stale within interval+margin; a
  correctly-idle producer does not alarm while a non-draining backlog does;
  the digest still sends templated when the LLM/fleet is down; the
  dead-man's-switch fires on a total outage.
- **§G:** `dream.min_interval_minutes=60` no-ops in-interval passes within
  one cadence, no redeploy; DB>env>default; per-producer cost attributes
  `claude_agent` spend by source; OpenRouter logs non-null cost.
- **§M:** `level:` 3-enum → two explicit fields; forward-only migration, no
  LLM-surface alias; a test pins that a job still leases and a todo never
  does.
- **§E:** each migrated throttle fires on the same cadence, single-flight
  preserved, interval declared once; no behaviour change beyond tick source.

## Explicitly NOT in scope

- Multi-class fairness scheduling (fair-share, gang, bin-packing, mid-run
  yield) — moot with one user.
- A dispatcher / singleton scheduler daemon — the claim substrate needs
  none.
- Interactive serving from a fused pool — the interconnect makes it batch.
- Routine force-kill as a responsiveness lever — reserve+drain and container
  teardown are primary.

## Decisions log & open questions

Decided (2026-08-02, Reto, unless noted): one unified master doc (sub-specs
stay mechanical); north-star = Postgres + web + worker + asa; one scheduler
folds every cadence via host-affinity; resumable-not-killable;
containerized dispatch in scope, greenfield-clean at the seams;
model-servers worker-spun on demand; isolation substrate = rootless podman
on the Sparks (Seatbelt/colima interim); git access pushed trusted-side;
file/blob storage git-first; todo↔job boundary stays (ADR 0030); §D↔§F share
one backlog signal; the scheduler pass is live — §A extends, not flips.

Open:

- **§A** — host-affinity representation: per-`Cadence` field vs a `prio`
  cell on the lease. Pin before building.
- **§H** — which passes containerize first (agentic + GPU are the wins);
  reuse the `job_claude_docker` seam or rebuild clean? Is any
  agentic/untrusted work pinned Mac-native long-term (⇒ invest in Seatbelt)
  or does everything route to the Sparks (⇒ interim only)?
- **§F** — materializer placement: one generic pass reading
  `(count-query, threshold, batch, resource)` from each `ServiceSpec`, vs
  per-producer minting; reshape vs leave-standing per producer.
- **§C** — needed at all? Gate on the one-Spark-quantized test.
- **§G** — knob shape (cadence recommended vs daily cap vs on/off); bundle
  the cost view or ship the knob first.
- **Files** — is the `folder` kind the blob-set container; the
  LFS-vs-regenerate line per artifact class.
- **Local flag** — taint-propagation completeness: enumerate every
  derivation writer and verify each carries the flag; where does the
  max-of-inputs computation live so a new writer can't silently skip it?
- Housekeeping (not this plan): `gr162141`, `gr55762` self-described shipped
  but still open — verify + close.

## Relationship to the sub-specs

This doc is the index + ordering + north-star; the sub-spec wins on
mechanics, this doc on cross-axis ordering and the laws.

- `gpu-priority.md` → §B (shipped; kept as the design record).
- `health-watchdog.md` → §D + Pillar 6's ladder.
- `compute-lane-lease-epoch.md` → §H-lifecycle (the epoch).
- `sim-harness.md` → §H's image-pinning + `precis_access:read`
  requirements (its slice 1 ships independently of this plan).
- `sandbox-run-substrate.md` → §H (built, dark; slices 2–3 = §H).
- `content-sensitivity-placement.md` → Pillar 5's must-stay-local guard —
  the one genuinely unbuilt piece of the routing cluster.
- `local-first-capacity-valve.md` → Pillar 5 activation — the first
  activation of the serving primitive's two dark pieces (`served_by` +
  `llm:` slots), for SMALL; gated on picking model M and measuring real
  `max_parallel` N; shovel-ready, ships as a unit.
- `factory-console-and-scheduling.md` → §K + the console/registry/capability
  detail; its scheduling framing is superseded by this doc.
- `gpu-cluster-modes.md` → §C — parked behind the one-Spark-quantized test.

Full wedge trail: `gr180096`.

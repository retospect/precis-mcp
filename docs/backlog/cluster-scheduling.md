---
status: draft
title: Cluster consolidation — one worker, one scheduler, one substrate; containerized, monitorable, elastic
model: opus
---

# Cluster consolidation (unified master plan)

> **The one plan to review.** Subsumes the scheduling framing previously
> scattered across `factory-console-and-scheduling.md` §15, `gpu-priority.md`,
> `gpu-cluster-modes.md`, `self-healing-spine.md`, and the related residual
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
`small-llm-derived-drain-band.md`); **containerized dispatch**
(`job_claude_docker`, colima sidecar, `sandbox_run` `mode:build`, and the
`plan_tick`/`fix_gripe` spawn seams that bypass `call_claude_agent`); §F
elastic residency for LLM models; §C fuse/split.

Cost is dominated by one producer: `dream` (~79 % of cluster LLM spend,
~$46/day) on a hardcoded 15-min timer — §G is the near-term fix.

## Pillar 1 — one worker, one scheduler, one control surface (laws 1–3)

- **§A — remaining cadences onto the live scheduler.** Fold the three
  fleet-singleton cadences — `dream` (gateway, 15-min), `reconcile` (caspar,
  daily), `anki_sync` (daily since 2026-09-26; 30-min when folded) — via a
  **host-affinity** field on `Cadence`
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
  remains spec** — activation via `small-llm-derived-drain-band.md` first.
  Open mechanics: generalize `local_serving`'s acquire/release to any
  `resource_slots` row; seed `llm:` rows from the host's real llama-swap
  model ids; residency is hysteretic (spin-up earned by a pile crossing
  high-water, released below low-water — never load/unload per call; a lone
  big call rides the cloud rung instead). Crash-safe reclaim shipped
  2026-08-10: `resource_slot_holds` TTL ledger + heartbeat sweep refunds a
  `local_serving` reservation a killed holder never released, fixing the
  fleet-wide `llm:*` `free=0` outage that day. Still open: the job-claim
  path (`workers/executors/_common.py` resource reservation) reserves
  without a hold, so a crashed job holder leaks its slot the same way —
  extend holds there or accept the gap.
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
  `self-healing-spine.md`.
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
- `self-healing-spine.md` → §D + Pillar 6's ladder.
- `compute-lane-lease-epoch.md` → §H-lifecycle (the epoch).
- `sim-harness.md` → §H's image-pinning + `precis_access:read`
  requirements (its slice 1 ships independently of this plan).
- `sandbox-run-substrate.md` → §H (built, dark; slices 2–3 = §H).
- `content-sensitivity-placement.md` → Pillar 5's must-stay-local guard —
  the one genuinely unbuilt piece of the routing cluster.
- `small-llm-derived-drain-band.md` → Pillar 5 activation — SMALL derived
  work (summarize/classify) minted as low-prio `derived_drain` jobs,
  melchior-pinned + router-capped at 6, **never cloud** (supersedes the
  retired `local-first-capacity-valve.md` spill design). Mechanism ships
  dark; activation = drop the SMALL cloud rung + flip the band flags.
- `factory-console-and-scheduling.md` → §K + the console/registry/capability
  detail; its scheduling framing is superseded by this doc.
- `gpu-cluster-modes.md` → §C — parked behind the one-Spark-quantized test.

Full wedge trail: `gr180096`.

---

# Absorbed 2026-09-26

## De-SPOF the melchior agent worker (incl. the silent-outage thread)

_Grouped 2026-09-26; was `agent-worker-despof`._

plan_tick and the whole claude lane run on one melchior claude_inproc worker;
a hang stalls the lane cluster-wide (observed: a 100-deep plan_tick queue
starving ad-hoc jobs — decide transient-vs-chronic with a draining-vs-growing
sample; the pass is default-on, `registry.py` `default_profiles=_AGT`). Ops
levers: provision a second agent host (caspar/balthazar) with the OAuth state
+ an agent daemon (no code); co-location relief — get the ~73 G mlock'd
llama.cpp weight off the agent host (or drop `--mlock`) so jetsam stops
targeting the worker. Durable north star: the sandbox_run/claude_docker
substrate (`sandbox-run-substrate` (git-only)) subsumes both. See also
spark-agent-worker for the local-lane offload.

Evidence gr187627: `ssh_node`'s blocking dispatch starves the claiming
worker's whole pass rotation for the compute's runtime (heartbeat dark,
host-dark criticals flap).

Update 2026-08-09 (dispatch-stall incident, alert 199905): redundancy is
confirmed **zero** — `service_config` has `job_claude_inproc` prio=0 on
spark since 2026-07-18, so melchior is the sole claude-lane executor
fleet-wide. Same incident exposed the deeper decoupling: `resource_slots`
advertising (heartbeat auto-probe, no flag) and executor provisioning
(profile + `service_config`) have no coherence check — a model served only
on a non-executor host (qwen3-235b on caspar) plus the plan_tick `llm:`
affinity stamp stalled the lane for 16 h. Mitigated by the 10-min
claim-side affinity fallback (`LLM_AFFINITY_GRACE_MIN`,
`executors/_common.py`); the structural gap (nothing connects "who serves
a model" to "who can run jobs that want it") is still open and belongs to
whichever de-SPOF lever gets picked.

Update 2026-08-19 — **topology changed; the SPOF did not.** The separate
`com.precis.worker-agent` daemon was retired ~07-22 and the agent profile
folded into the single `com.precis.worker` unit (`precis worker --profile
all --batch-size 32 --idle-seconds 2`); `job_claude_inproc` has run inside
it since ~08-07 18:34. Melchior is still the sole claude-lane executor, so
every lever above still applies — but the *diagnostic surface* moved:
`/var/log/precis-worker-agent.log` is a dead 0-byte file and the missing
daemon is expected, both of which read as "worker is dead" to a fresh
investigator (two agents in one session drew exactly that wrong
conclusion). Worth a line in whatever runbook covers the lane.

Live now: lane alive and polling, `claimed=0`, ~102 claude_inproc jobs
queued since 08-16 00:12 UTC never claimed — a **selection** failure, not
an outage, so the watchdog note below (dispatch-stall detector on
expired-lease job refs) should also cover "queue depth grows while
claimed=0", which no restart fixes.

Silent-outage thread (merged from agent-worker-silent-outage): melchior
`com.precis.worker-agent` was SIGKILL'd and stayed dead ~4 days
(2026-07-26→30), silently stalling all agent-profile work. Root-cause the -9
(jetsam/OOM/crashloop — the mlock'd llama.cpp weight above is the prime
suspect) so it can't recur silently; the deferred H1/H3/H4 reliability track
(memory `worker-agent-silent-outage`) is the same thread. Related watchdog:
verify the nursery dispatch-stall detector fires on expired-lease job refs.

## Run the local-model job lane on a spark worker, not melchior

_Grouped 2026-09-26; was `spark-agent-worker`._

The claude_inproc pin to melchior is obsolete for operations steered onto the
BIG chain: the in-process openai_tools loop needs only a DB connection and
HTTP reach to the local endpoint — no claude binary, no OAuth. Reto's call:
make a serving spark (castor/pollux) a precis worker pulling these jobs. Work
out: node choice (the worker competes with llama.cpp RPC cores; the
nice-all-jobs core reservation is the lever); eligibility = "can reach the
local endpoint", not a hostname (the scheduler's `eligible` callable), else
it trades one SPOF for another; the residual claude lane (fix_gripe etc.)
must keep landing on melchior — a second eligible host for one lane, not a
profile migration. Owner `deploy/` host profiles +
`src/precis/workers/registry.py`.

test: a plan_tick/briefing job completes on a spark-node worker with no OAuth
credential present.

## GPU cluster topology modes

_Grouped 2026-09-26; was `gpu-cluster-modes`, status draft, blocked-by gpu-priority._

> **PARKED (2026-08-02)** — this is §C (GPU topology) of
> `cluster-scheduling.md`, and the master **gates fusion on the
> one-Spark-quantized test**: one Spark's ~119 GB serves ~120 B @ 8-bit /
> ~200 B @ 4-bit, so if your models fit, none of this is built. The
> disaggregated half is simply the master's default (no doc needed). Do not
> build from this spec until that test fails; also `blocked-by: gpu-priority`.

### Motivation / why

Soon there are N DGX Spark units (1 today + 3 incoming). The same hardware
serves two mutually-exclusive shapes:

- **Aggregated:** the units fused into one large accelerator (~N × 119 GB
  unified) running a really large model, fronted by one node (A).
- **Disaggregated:** N independent nodes each doing small/independent work
  (CFD, catpath seeds, per-node models, embedding).

This proposal specifies how precis *chooses* and *switches* between those
shapes. It is the elastic-topology layer above the per-node slots from
`gpu-priority.md` (which must exist first — hence `blocked-by`).

**Read the "When fusion is (and isn't) worth it" section first** — it may
argue you need less of this than it looks.

### Hardware reality — the constraint that shapes everything

Spark units cluster over **ConnectX Ethernet/RDMA (~200 GbE), not NVLink**
between chassis. So a fused model is **interconnect-bound**:

- **Tensor-parallel** (all-reduce every layer) is chatty and punished by an
  Ethernet fabric → poor fit.
- **Pipeline-parallel / expert-parallel** (activations between stages) is far
  more forgiving → the realistic way to shard across Sparks.

Consequence, and it's load-bearing: **the fused pool is a throughput /
batch engine, not a low-latency one.** Per-token latency across 4
Ethernet-linked Sparks is high. So the fused model is right for *"queue a batch
of hard reasoning jobs and drain them"* and wrong for *"chat with a 400B model
snappily."* Design for batch `bigpool` jobs, not interactive big-model serving.
(Verify the exact interconnect + the serving stack's PP support before
committing — see Open questions.)

### When fusion is (and isn't) worth it — the honest counter

One Spark's ~119 GB unified memory **already serves large quantized models** on
its own (order ~120 B @ 8-bit, ~200 B @ 4-bit) via the existing llama-swap
path. **Fusion only earns its complexity for frontier-size models you cannot
quantize onto a single unit** (~400 B+ at usable precision). So:

- If the models you actually want fit on one Spark quantized → **don't build
  fusion**; serve them per-node and use the disaggregated cluster for
  everything. This whole proposal stays on the shelf.
- Fusion is worth it only when you have a standing need for a model too big for
  one unit. That need is likely **occasional**, which is the strongest argument
  for *manual, rare* aggregation over frequent autonomous switching (below).

Decide this first. It gates whether any of the rest is worth building.

### The abstraction — schedule topologies, not nodes

The broker picks a **mode**; each mode publishes its slot inventory to the one
queue:

- **Disaggregated** → N independent single-node `gpu` slots (the `gpu-priority`
  slots, unchanged).
- **Aggregated** → 1 composite `bigpool` slot (the fused units, via node A).

Jobs declare `requires={"gpu"}` (any one node) vs `requires={"bigpool"}`. Modes
are mutually exclusive. **"Models pull the jobs" — already how precis works:**
the aggregated pool is a *transient consumer* that comes into being when the
mode flips, claims and drains `bigpool` jobs "for a while," then dissolves so
the nodes rejoin the disaggregated pool. The broker only decides the mode and
runs fuse/defuse; the existing pull-claim drains whichever slots are published.

### Start manual, earn the autonomy

Given single-user + likely-occasional fusion, do **not** open with an
autonomous demand-batching controller. Start with a **human topology toggle**,
mirroring `gpu-priority`'s reserve mode:

- `precis cluster fuse` → drain disaggregated work to a low-water mark, stand up
  the pool, serve `bigpool` jobs.
- `precis cluster split` → drain in-flight `bigpool` jobs to a boundary, tear
  the pool down, resume disaggregated.

This covers "I have big-model work, bring it up; I'm done, give me the cluster
back" with almost no control logic. **Autonomous demand-driven switching**
(aggregate when queued `bigpool` demand crosses a threshold or a job ages past a
deadline; defuse under disaggregated pressure) is a *later* phase, added only if
manual proves tedious.

### The control problem — hysteresis (only once autonomous)

Fusing + loading a frontier model across N units over RDMA is **minutes**, as is
teardown. So autonomous switching must **batch**: aggregate only above a demand
threshold (or human reserve, or job deadline); drain to a low-water mark; switch
back only under disaggregated pressure; **min-dwell timers** in each mode prevent
flip-flopping. Mis-tuned, this is thrash at a coarse grain. This is the same
turn-taking law as a small model holding a node — just a much larger stretch and
entry threshold.

### Hard parts (consider these before building)

- **Interconnect-bound latency** (above) — the fused model is batch, not
  interactive. Don't sell it as interactive big-model chat.
- **Pool-node failure.** One unit dies mid-aggregate → a missing PP/TP shard →
  the whole model is down. Need health + graceful handling: fail+requeue the
  in-flight `bigpool` jobs, and either revert to disaggregated or stand up a
  smaller pool (N-1 units, smaller model). The `gpu-priority` lease/orphan-reaper
  extends: a dead pool member releases the *whole* pool cleanly.
- **One big model at a time.** Serving multiple frontier models adds a second
  reload axis (which model is loaded) → more thrash. Simplest: one configured
  `bigpool` model; switching models is itself a mode-internal reload. Multiple
  big models = deferred.
- **Interruptibility, one level up.** A `bigpool` job mid-inference can't be
  preempted; a switch-back waits for its checkpoint. So `bigpool` jobs must be
  bounded/checkpointed too (same law), and defuse only at a low-water mark, never
  per-job.
- **Fuse/defuse is a pluggable driver.** `aggregate(nodes) -> endpoint` /
  `disaggregate()` behind a clean interface; the mechanics (serving stack — vLLM
  pipeline-parallel? SGLang? llama.cpp RPC? — RDMA/NCCL init, health, fronting
  through A) live there. The broker stays topology-agnostic so the serving stack
  can evolve without touching scheduling.

### The slurm line

In scope: **few coarse modes** (disaggregated / aggregated; maybe one
intermediate), demand-driven-or-manual switching, hysteresis, coarse cross-mode
windows/deadlines for fairness. Out (this is what makes it slurm): combinatorial
scheduling over arbitrary node subsets, per-job fair-share accounting, gang
scheduling of arbitrary groups, true mid-inference preemption. Keep modes few
and switches coarse.

### Phasing

1. **Manual topology toggle** — `fuse` / `split` commands: drain, stand up / tear
   down the pool via the driver, republish slots. Plus the `bigpool` slot type +
   `requires={"bigpool"}` job routing. (Depends on `gpu-priority` per-node slots.)
2. **The fuse/defuse driver** for the chosen serving stack (the real infra:
   RDMA/NCCL init, PP sharding, health, node-A front).
3. **Autonomous demand-driven switching** with hysteresis — only if manual is
   insufficient.
4. **Pool-node-failure handling** (fail+requeue, degrade to smaller pool).

### Explicitly NOT in scope

- Interactive low-latency serving *from the fused pool* (interconnect makes it
  batch — serve interactive models per-node instead).
- Combinatorial / fair-share / gang scheduling (the slurm line).
- Multiple simultaneous big models; arbitrary node-subset pools.
- Anything, if the "when fusion is worth it" test says one-Spark-quantized
  covers your models.

### Acceptance criteria

- `precis cluster fuse` drains disaggregated work to a low-water mark, stands up
  the pool, and `requires={"bigpool"}` jobs run against it; `split` tears it down
  cleanly and disaggregated work resumes — no orphaned GPU reservations either
  way.
- A `bigpool` job and a disaggregated `gpu` job cannot both hold a unit at once
  (modes are mutually exclusive; verified).
- A killed / dead pool node releases the whole pool and re-queues its in-flight
  `bigpool` jobs (injected-failure drill).
- (If autonomous) the pool does not flip modes more than once per min-dwell
  under a mixed demand workload.

### Target + blast radius

- New: a topology-mode selector + the `bigpool` slot type; `requires={"bigpool"}`
  routing; the `fuse`/`split` CLI.
- The fuse/defuse **driver** (serving-stack-specific; likely a new deploy role +
  a runtime control plane) — the bulk of the real work.
- Reuses `gpu-priority`'s per-node slots, leases, orphan-reaper, PRIO, reserve
  mode; `resource_slots` for the composite slot.
- Node A serving front (llama-swap / the chosen stack).

### Open questions / decisions log

- **Is fusion even needed?** Run the one-Spark-quantized test against the models
  you actually want. If they fit, shelve this. (Gate on everything.)
- **Interconnect + parallelism** — confirm ConnectX/RDMA topology and that the
  serving stack does pipeline/expert-parallel well enough over it to be useful.
- **Serving stack for the pool** — vLLM PP / SGLang / llama.cpp RPC / other; this
  choice drives the driver.
- **Trigger** — manual-only for a long time, or is there real autonomous
  `bigpool` demand (system jobs, not just the human) that justifies the
  demand-batching controller?
- **Model set** — one configured big model, or a switchable set (adds reload
  thrash)?

### Relationship to `gpu-priority.md`

`gpu-priority` builds the per-node slots, leases, reserve mode, and `PRIO`
claim — the disaggregated substrate. This proposal adds the *composite* slot and
the mode selector on top. It cannot start until those exist, and it should not
start at all until the fusion-worth-it test passes.

## Embedder-as-service + image split

_Grouped 2026-09-26; was `embedder-service-and-image-split`._

Shipped portion: see ADR 0020 and the
`src/precis/embedder_service.py` / `embedder_wire.py` module
docstrings; full plan in git history. Live: `precis serve-embeddings`
(native launchd + MPS on Macs, CUDA container form on Linux),
`RemoteEmbedder` client with exponential backoff, the shared
monorepo wire schema, torch removed from the serve/worker images
(the `ingest` image keeps Marker/torch), idle-unload residency
(`PRECIS_EMBEDDER_IDLE_S`, cluster-scheduling §F). Decided: every
node runs its own local embedder; cross-node forwarding is fallback
only.

### Open scope

- **`chunk_keywords` embedder-load follow-up** (not v1-blocking,
  revisit with the service live): the pass embeds ~40 candidate
  phrases per chunk — heavier than the embed pass itself. Cross-chunk
  batch coalescing + a phrase→vector cache (phrases repeat across
  chunks/papers) would cut remote round-trips.
- **Wire format** — stay JSON, or msgpack the float payload? Decide
  if payload size ever shows up in profiles.
- **Auth on forwarded endpoints** — bearer token vs tunnel identity.
  Moot in the all-local topology; only matters if a node ever borrows
  another's embedder.
- **Image split refinement (deferred):** split `ingest` back out of
  `worker`, or fold a CPU-only worker into `serve`'s base — the
  shipped serve/worker/ingest/embedder table is the v1 target.

## Fair dispatch — one candidate-picker, two cost currencies, user-first

_Grouped 2026-09-26; was `fair-dispatch-two-currencies`, status draft._

### Motivation / why

Review findings (2026-08-08), plus the operator's stated allocation policy.
Evidence: gr191337 (taproot_backfill monopolizes the claude_inproc lane for
hours), gr191125 (band-5 starves behind re-minted band-2 cron), gr200375
(fetch_oa monopolizes the serial melchior loop).

**Fairness defects in the dispatch lane** (`src/precis/workers/dispatch.py`):

1. `_candidate_parent_ids` orders `ORDER BY r.ref_id LIMIT 50` — the
   head-of-line starvation trap already fixed twice elsewhere
   (`auto_check.py` → `ORDER BY random()`, with the rationale in its
   docstring; the doable view → least-served rotation). ~~Latent today
   (~6 effective candidates in prod, 242 raw auto-run todos)~~ — **NO
   LONGER LATENT, confirmed live 2026-08-19.** The predicate returns **86**
   candidates; melchior runs `--batch-size 32`, so the page is 32, not 50.
   Two freshly-minted `taproot_backfill` todos (218295 / 218296, root
   todos, fully eligible — no blocking child, no exclusion tag, no
   schedule) sit at queue positions **85 and 86** and are therefore
   unreachable on every pass, indefinitely. The user-visible symptom is
   "I queued work and nothing ever happened", with no attention tag and
   no failed job to explain it — the todo just stays `STATUS:open`
   forever, which is the worst possible failure signature.

   Compounding it: **all 86 candidates currently have zero live child
   jobs** — the head of the queue is not churning, so the same oldest 32
   re-occupy the page every pass and nothing behind them ever advances.
   Whatever causes the zero-mint is a separate defect (under
   investigation), but it converts this ordering flaw from "slow" into
   "permanently stuck", which is the argument for fixing the picker even
   before the mint bug is understood. Aging or `random()` would have let
   the tail through regardless of the head's state.
2. `prio` is honored at job-*claim* time
   (`executors/_common.py::claim_executor_jobs`, `COALESCE(prio,5), ref_id`)
   but ignored at *candidate* time — an urgent parent past position 50
   never mints, so the slice-6a prio plumbing is undermined one stage
   upstream.
3. The daily-ceiling cadence exemption (`_cadence_parent_ids`) filters
   *within* the ref_id-ascending page. Cadence ticks are freshly-minted
   children → highest ref_ids → tail of the page: under a ≥50 backlog
   the exemption that exists to protect the morning brief (2026-08-07,
   six hours unminted) cannot see it. A tripped ceiling is now the
   routine daily state (state-map §guardrails), so this path is
   load-bearing.
4. Budget consumption is first-come-first-served: within the daily
   envelope, whichever tree enumerates earliest spends; ~5 trees at the
   $10 tree cap drain a $50 ceiling before others tick once. The human
   lane already has least-served rotation
   (`_todo_views._fetch_doable`: `ORDER BY prio, (picks_7d+1)/(1+w)`);
   the robot lane has none.
5. Maintenance hazard feeding all of the above: ~100 lines of
   eligibility SQL duplicated by hand between `_candidate_parent_ids`
   and `_claim_and_dispatch`, symmetric only by discipline.

**The two-currency problem.** The guardrails already learned half of it
(migration 0112: `placement='local'` rows are excluded from the $ caps —
cluster GPUs are sunk cost). The scheduling layer never learned the other
half: local capacity is a *resource-allocation* problem, not a spend
problem, with its own policy —

- **Work-conserving**: the cluster should always be busy; a tripped
  cloud-$ ceiling must never idle local slots.
- **Good mix**: local slots shared fairly across roots, not drained by
  whichever tree enumerates first.
- **User-first, no preemption**: when the operator is actively working,
  their jobs claim ahead of background work — running jobs finish, new
  claims prefer the user. (Direction already pinned for GPU work:
  `gpu-priority.md` human-first claim + reserve mode, shipped.)

### In scope

1. **Shared eligibility-SQL builder.** One function renders the
   dispatch-eligibility predicate, parameterized enumerate vs lock
   (`FOR UPDATE OF r SKIP LOCKED`); `_candidate_parent_ids` and
   `_claim_and_dispatch` both call it. Deletes the hand-kept symmetry.
2. **One candidate-picker policy, shared across sweeping passes.**
   Ordering: `COALESCE(prio,5) ASC, <least-served root> ASC, random()`.
   The fairness term is per-strategic-root service over a trailing
   window, reusing the doable-view rotation shape (picks or recorded
   spend per root — open question 1). Applied to: dispatch candidate
   enumeration, `schedule/worker.py::_candidate_recurring_ids` (drop its
   `ORDER BY ref_id`), and offered to future sweeps as the default.
   `auto_check` keeps its random sample (already fair; no prio concept).
3. **Cadence candidates enumerated separately.** A dedicated query for
   ticks-under-a-`meta.schedule`-watch, unioned ahead of discretionary
   candidates — the exemption stops depending on page position. Fixes
   finding 3 structurally.
4. **Two-lane budget gating.** Classify each candidate's next job as
   local-bound or cloud-bound at dispatch time (open question 2). The
   global daily ceiling (`planner_guardrails.daily_budget`) gates
   **cloud-bound discretionary** candidates only; local-bound candidates
   dispatch whenever slots are advertised (`resource_slots` /
   `llm_serving.py`) — work-conserving. The per-todo/per-tree $ caps
   keep their existing 0112 semantics (cloud rows only). Local fairness
   comes from the picker's least-served term, not from $ math.
5. **User-first claim window.** A user-activity signal (open question 3)
   sets a short-TTL "interactive" flag; while set, the job claim in
   `claim_executor_jobs` and the dispatch picker strictly prefer
   `prio<=2` (user/chat/cadence) work and throttle background minting
   (skip discretionary dispatch when local slots are ≥N-1 busy). No
   kill, no preemption — running jobs finish; this is claim-order only.
   Coarse manual override stays: reserve mode (`gpu-priority.md`).
6. **Auto-run signal consolidation.** Retire the dead
   `executor:<runner>` tag branch in `_claim_and_dispatch`
   ("Reserved; v1 has no registered executor:* values") and its arm of
   the three-way OR in every eligibility query. Prod-scan for
   `meta.executor`-only writers; keep `meta.executor` reading as
   back-compat but the eligibility predicate collapses to
   `meta ? 'llm_tier' OR meta ? 'executor'` (single builder site after
   item 1, so this is one edit).
7. **Schedule/scheduler disambiguation.** `run_schedule_pass` gets one
   declared trigger: keep the scheduler's `cron_tick` lease (exactly-once
   across the fleet), drop the copy in the default worker rotation.
   Rename `workers/scheduler.py` → `workers/cadence.py` (or
   `workers/schedule/` → `workers/recurring/` — pick one at build time)
   so the two subsystems stop colliding on grep.
8. **Unified blocked-state module.** One registry of block reasons —
   each entry = reason id + SQL fragment + human label — covering the
   four mechanisms that today live apart: STATUS gating, the
   `_DOABLE_EXCLUSION_TAGS` open-tag registry, child-liveness
   (`_parked_child_still_blocks_sql`, incl. the parked-bypass/hard-block
   split), and job-status blocking (`_job_blocks_dispatch_sql`). The
   item-1 eligibility builder composes its predicate from this registry
   (NOT the other way round — the registry is the single source); the
   doable view, nursery stuck-doable check, and attention view render
   their "why is this blocked" strings from the same entries. Extends
   the pattern `_DOABLE_EXCLUSION_TAGS` already proved ("adding a new
   exclusion form means appending to the registry, no SQL edits").

### Explicitly NOT in scope

- Preemption / killing running jobs (reserve-mode kill backstop already
  exists for the GPU case; unchanged).
- Multi-class fairness scheduling, weights config, or per-user
  accounting — one human user; the picker's single least-served term is
  the whole mix policy.
- Changing the guardrail caps themselves (values, 0112 placement
  semantics, `daily_budget` single-source contract with the scheduler).
- The melchior single-`claude_inproc`-worker SPOF (OPEN-ITEMS item;
  orthogonal — this proposal fixes what gets *minted/claimed first*,
  not throughput).
- Dropping `meta.executor` *writes/reads* entirely (item 6 keeps the
  key as back-compat; a full migration of legacy writers is a follow-on).
- The nursery digest's own `ORDER BY ref_id LIMIT 50` pagination
  (visibility, not execution — item 8 gives nursery the shared reason
  vocabulary, not new pagination) — OPEN-ITEMS "Dispatch-review
  residuals" entry.

### Acceptance criteria

- One eligibility-SQL builder; `dispatch.py` contains no duplicated
  predicate blocks (grep: `_parked_child_still_blocks_sql` referenced
  from the builder only).
- Test: with >limit eligible candidates, a prio=1 candidate beyond the
  old page position mints in the first pass.
- Test: with >limit eligible discretionary candidates and a tripped
  ceiling, a cadence tick (highest ref_id) still mints.
- Test: two roots, one with heavy recent service — the starved root's
  candidate mints first at equal prio.
- Test: tripped daily ceiling + advertised local slots → a local-bound
  candidate mints; a cloud-bound discretionary candidate does not.
- Test: interactive flag set → a prio=5 background job is not claimed
  while a prio=1 job is queued; flag expiry restores normal order.
- Schedule pass: >limit recurrings → every recurring is inspected
  within k passes (no deterministic tail starvation).
- Eligibility SQL has a single auto-run predicate; grep for
  `executor:%` in `dispatch.py` returns nothing.
- Exactly one caller of `run_schedule_pass` outside tests; no module
  named both `schedule*` and `scheduler*` under `workers/`.
- Block-reason registry is the only definition site: grep finds
  `_parked_child_still_blocks_sql` / `_job_blocks_dispatch_sql` logic
  only inside the registry module; nursery stuck-doable and the
  attention view render reason labels from registry entries (test:
  adding a registry entry surfaces in dispatch SQL, doable exclusion,
  and nursery reason string without further edits).

### Target + blast radius

- `src/precis/workers/dispatch.py` (builder, picker, cadence union,
  lane gate) — highest risk; gr192606-class runaways guard against
  regression via existing tests.
- `src/precis/workers/executors/_common.py::claim_executor_jobs`
  (interactive-window preference).
- `src/precis/workers/schedule/worker.py` (picker ordering; item 7
  rename + single-trigger touches `workers/registry.py` and
  `workers/scheduler.py`).
- `src/precis/workers/planner_guardrails.py` (ceiling verdict gains
  lane awareness; `daily_budget` contract unchanged).
- New: user-activity signal write path (MCP/asa touchpoint or manual
  toggle) + small helper module for the picker.
- New: block-reason registry module (item 8) —
  `handlers/_todo_views.py` (doable exclusion + attention view),
  `workers/nursery.py` (stuck-doable reasons) become consumers.
- Docs: `state-map.md` todo-tree/guardrails sections;
  `cluster-scheduling.md` cross-reference (this is its law-3/law-1
  refinement for the dispatch lane).

### Open questions / decisions log

1. **Fairness term: picks or spend?** Per-root `status:done` events 7d
   (doable-view shape, cheap, already indexed) vs per-root
   `llm_call_log` spend (truer for cost, splits naturally by placement
   for the two lanes). Leaning: picks for the local lane (slot-time ≈
   job count), cloud-$ for the cloud lane.
2. **Local-vs-cloud classification at dispatch time.** The router
   resolves placement per rung dynamically (`router._placement_of`,
   chains can spill). Candidate classification must be a cheap static
   approximation — proposal: classify by the todo's tier/operation
   default chain head (local-served model advertised in
   `resource_slots` ⇒ local-bound), accept that a spill-to-cloud after
   dispatch bills the envelope retroactively (caps still bound it).
3. **User-activity signal.** Options: (a) manual toggle only (reserve
   mode generalized, zero false positives), (b) TTL heartbeat on
   MCP-session verb traffic (automatic, but session MCP hits prod —
   needs a write-cheap path, e.g. `host_heartbeat`-style row), (c) both
   — toggle authoritative, heartbeat advisory. Leaning (c).
4. **Does the interactive window throttle cadence work too?** Leaning
   no — cadences are the user's own deliverables (the 2026-08-07 lesson).

### Live incident 2026-08-19 — cadence-exempt spend pins the ceiling permanently

Root cause of the zero-mint noted in finding 1 above, now identified.
**Discretionary dispatch has been paused continuously since 2026-08-16
06:04 UTC** (~3.5 days) by the global daily cost ceiling:

```
dispatch: daily ceiling ($59.85 >= $50.00) — discretionary dispatch paused
```

The pass itself is healthy and runs every ~10-15 min (`dispatch claimed=0
ok=0 failed=0`, last 2026-08-19 07:33:57 UTC); 53 ceiling-hit logs between
08-16 06:04 and 08-17 19:15. Cadence + zero-LLM candidates stay exempt, so
recurring work (news_poll, briefing, card_forge) kept succeeding the whole
time — which is exactly why this reads as "everything is fine" from the
outside while every non-recurring executor todo silently never runs.

**The window is trailing-24h, not calendar-day, so it does not self-clear
— and the exempt work is what holds it open.** `dispatch.py`'s own comment
already names the pathology from a prior occurrence: "Cadence-exempt quest
ticks kept the trailing-24h window over the ceiling permanently, which
starved every `autocatpath_aggregate` mint for 29h (2026-08-16/17)". That
29h incident has now recurred as a 3.5-day one and should be treated as
chronic, not incidental: exempt spend alone exceeds the envelope, so the
gate that is supposed to throttle discretionary work has become a
permanent off-switch for it.

This is a **third** fairness currency the design above doesn't yet cover:
not cloud-$ vs local-slots, but *exempt vs discretionary claim on the same
$ envelope*. Options, in rough order of structural merit:

1. Reserve a discretionary floor — cadence/zero-LLM exempt work may not
   consume more than X% of the envelope, so discretionary always retains a
   slice. (Directly kills the self-sustaining lockout.)
2. Charge exempt work to a separate envelope, so cadence spend cannot move
   the discretionary gate at all.
3. Age-based override — a candidate starved beyond N hours mints regardless
   (bounded, one job), so nothing is *indefinitely* invisible.
4. Raise `PRECIS_DAILY_COST_CEILING`. Treats the symptom, and the trailing
   window means it will re-pin at whatever the new value is.

Whatever is picked, the **observability gap is the urgent half**: a
ceiling-paused todo shows `STATUS:open`, no attention tag, no failed job,
no user-visible signal of any kind. It is indistinguishable from work that
simply has not been reached yet. At minimum, a todo skipped by the ceiling
should carry a visible reason tag, and `/status` (or the nursery digest)
should surface "discretionary dispatch paused, N candidates waiting, oldest
Nh" as a first-class state.

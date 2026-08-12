---
status: draft
title: Self-healing spine — two registries, one report, one doctor agent
model: opus
---

# Self-healing spine

**The single plan for cluster self-healing, health reporting, and the
fix-it agent.** Folded in and superseded (files deleted, this is the
umbrella; decision 2026-08-12):

- `HANDOFF-claim-liveness.md` (repo root, 2026-08-12) → Layer 1.
- `docs/backlog/health-watchdog.md` phases 3–5 → Layers 2–4.
- `docs/backlog/fleet-watching-agent.md` T0/T1/T3 → Layers 2–4
  (T2 governor deliberately parked, see Deferred).
- `docs/backlog/nursery-missing-pass-detector.md`,
  `docs/backlog/nursery-digest-sampling.md` → condition-registry rows.

Shipped present-state (health_digest phases 0–2, nursery detectors,
remediation router, dead-man's switch) lives in the owning module
docstrings, not here; original designs in git history of the deleted
files.

## The diagnosis (why this doc exists)

The cluster already self-heals in ~17 places, but as bespoke mechanisms
that each grew from one incident: **five** distinct "is it dead"
predicates (TTL expiry, lease expiry, boot-epoch mismatch, wall-clock tag
age, PID liveness), **five** claim types with five recovery stories
(the fifth surfaced 2026-08-12: coordinator `quest_tick` jobs opt out
of lease-steal reclaim entirely — their only rescuer is the
`quest_loop_reconcile` pass on melchior),
**two** detector passes declaring checks in two shapes, a dozen
hand-rolled backoff idioms, several disjoint report surfaces, and an
escalation ladder (alert → page → gripe → fix_gripe todo → agent) that
exists as three disconnected half-ladders. Recovery from a worker SIGKILL
during deploy ranges from seconds (job lease, epoch arm) to 1 hour (slot
hold TTL) to never (zombie agentlog) depending only on which claim type
held the resource.

The mature piece is the **job-lease boot-epoch reclaim**
(`workers/executors/_common.py`: mint per-process `boot_id`, advertise in
`host_heartbeat.meta.boot_ids`, reclaim when the generation is *provably
replaced* — no TTL wait). The unification: that becomes the one liveness
story for every claim; one condition registry becomes the one detection
story; one reporting spine replaces the scattered digests; LLM judgment
(the doctor) sits at the top of one ladder — never inside detection.

SLA framing (kept from health-watchdog): "never really urgent — just
don't let it linger and rot for days." Nursery's lane owns real-time
critical; everything else is slow-rot.

**Dark-factory framing** (`docs/mission.md`): a factory that runs with
the lights off has no human on the floor to notice a jammed machine, so
it must notice and clear its own jams or it silently degrades until a
human walks in. Layers 0–2 are the interlocks and PLC watchdogs
(deterministic, alive when everything else is down); the doctor is the
maintenance engineer doing rounds; the escalation ladder is the andon
cord, pulled only when the factory truly can't fix itself. Crucially the
spine is built *from the factory's own machinery* — the doctor is a
plain agent-lane job type, and its fixes flow through the same
gripe→groom→fix_gripe rails as ordinary work — so self-repair is a
product workload, not a parallel bespoke system. The autonomy dial
(report→heal→draft→ship) is the same trust-earning progression that
turns the lights further off.

## Architecture — four layers + one report, each ONE mechanism

### Layer 0 — structural non-failure (exists; the preferred layer)

Derived-queue ("a missing derived row IS the queue entry — an outage
delays, never loses"), Postgres session advisory locks (`ingest/claim.py`
needed *no* reaper by construction), supervisor `KeepAlive`/`Restart=`.
**Design law: before adding recovery for a failure class, try moving it
here.** No new code; this layer is the yardstick.

### Layer 1 — the claim registry (deterministic reclaim)

One identity, one predicate, one reaper.

- **Identity**: every claim a process can die holding carries
  `(host, process, boot_id)`. Today only job leases do. Add it to
  `resource_slot_holds` (migration) and to agentlogs at `open_log`
  (`meta.worker`). Claims minted outside a worker (CLI) carry NULL and
  fall back to TTL — same asymmetry the job lease already documents.
- **Predicate**: extract the epoch test (including the
  no-advertised-boot-id sentinel and the "unadvertised worker must not
  stamp identity" guard) from `_common.py` into one shared helper.
  Extract-and-share, never fork — the COALESCE subtleties are hard-won.
- **Reaper**: one sweeper phase driven by a declarative list of claim
  types, each row = {locate orphans, epoch arm action, TTL arm action,
  forensic tag}. Initial rows: slot holds (epoch → delete hold + credit
  free; TTL arm = existing sweep), agentlogs (epoch → finalize
  `status='aborted'`), DFT/job containers (existing watchdogs register
  for uniform reporting), dead-node ssh_node orphans, generic stuck-job
  sweep. **Job leases keep their in-claim machinery untouched** (it must
  stay in the claim path for the starvation-bound pre-pass); they
  register read-only so reporting is uniform.
- **Uniform forensics**: one grep-able line per reclaim — claim type,
  arm (epoch|expiry), owner identity, age — and the existing distinct
  tag namespaces (`swept:` / `reaped:` …) preserved.
- **Stop creating orphans** (handoff Part B): SIGTERM aborts the
  in-flight LLM stream between SSE chunks → finalize partial → release
  hold → exit batch loop; `TimeoutStopSec` on worker units sized to the
  drain; `redeploy-precis.yml` restarts each unit **once** per run
  (collapse the repeated restart-notifies).

Deliberate exceptions, renegotiated not silently kept: `coordinator`
(no lease reclaim today — `coordinator-crash-recovery.md`), the quest
reconciler's faster orphan path (keep as a registry row with a tighter
cadence — `quest/loop.py` documents the division of labor — **but it
must register**, not stay bespoke: 2026-08-12 evidence shows the
rescuer's cadence is load-dependent and single-host — four orphaned
quest loops waited ~30 min because melchior was grinding a `bib_parse`
backlog, so recovery hinged on one busy host's pass-loop latency.
Registering puts the rescuer's own cadence under the staleness watch).

### Layer 2 — the condition registry (detect → heal → escalate)

`nursery` (per-minute, critical lane) and `health_digest` (hourly,
slow-rot lane) already converge on the same alert lifecycle and the same
remediation router. Keep both cadence lanes and the zero-LLM invariant;
unify the **declaration**: a condition is one registry row —

```
Condition = { probe (SQL), lane (fast|hourly), severity,
              idle_aware (alarm on not-draining, never on quiet),
              remedy: heal(action, bounds) | gripe | page }
```

- **`heal` actions are whitelisted and bounded.** Extract the sweeper
  unpark counters (`attempts` + exponential cooldown + hard cap +
  terminal latch tag + one aggregate finding for the capped set) into a
  shared `bounded_heal` primitive — the repo's proven "self-heal with
  human handoff" shape, today inline-only. First actions:
  transient-failed reopen (exists — becomes a row), unpark (exists —
  becomes a row), **restart-once** for daemon-death findings (new; the
  capped answer to the embedder lesson: a restart watchdog that keeps
  kicking a wedge must escalate, so cap=1 then gripe), requeue-stranded
  (exists in paper_hygiene — registers). **restart-once prerequisite**
  (exposed by gr204385: a wedged worker can't bounce itself, and the
  agent path was permission-denied): a cross-host vetted primitive —
  ssh + per-host sudoers grant scoped to exactly
  `launchctl kickstart -k system/com.precis.worker` (systemd analog on
  spark) and nothing else.
- **`gripe`** = the existing condition-fingerprinted auto-closing marker
  gripe router with per-class self-heal budgets. Unchanged; the
  registry's default escalation for anything `heal` can't clear inside
  its budget. (`backlog_groom` stays nudge-only for watchdog gripes per
  the SLA, unless a class earns auto-groom.)
- **`page`** = existing `notify_critical_alert` (and set
  `PRECIS_OPS_ALERT_TARGET` on system workers — the critical push is
  dark today).
- **New rows, not new mechanisms** (absorbing fleet-watching T1 and the
  two nursery items):
  - per-lane end-to-end probes generalizing `embed-lane-stalled`
    (jobs of lane L queued AND zero L successes over the window);
  - drain-stall (pending of type T > high-water AND zero completions
    60 min → alert + pause the materializer's minting for T);
  - swap > threshold on the pg host; expected-daemon-dead;
    clock skew > 5 min;
  - `llm_call_log` degradation (error-rate / latency / parse-failure per
    (model, transport, placement) — columns exist, nothing reads them as
    health);
  - env-gated pass silently absent from a live rotation for N hours
    (the quest_loop_reconcile scar);
  - rescue-critical pass cadence over budget (pass-loop starvation: one
    greedy pass starves every other pass on its host — 2026-08-12,
    `bib_parse` stretched quest_loop_reconcile to ~30 min on melchior
    while caspar sat wedged 9 h in `_bib_parse_pass` with a stale
    heartbeat; rescue passes get an explicit cadence SLO, and a wedged
    pass with stale heartbeat is a restart-once finding);
  - surface canaries (PCB/CAD/export builds): last-use-ok + dep-present
    first; weekly synthetic canary later. Never alarm on mere
    absence of use.
- **Fair sampling on capped detectors**: backlog-style probes replace
  `ORDER BY ref_id LIMIT 50` with oldest-first-by-staleness or random
  sample + true `COUNT(*)` (partly shipped d4b0d354); test: item #51
  surfaces within k digests.
- The scattered cadence/single-runner idiom (`app_state` marker +
  `pg_try_advisory_xact_lock`, repeated in ~8 passes) becomes one
  library helper the registry uses.

### Layer 3 — the doctor (LLM judgment; "the agent that goes around")

Not a daemon and not a new substrate: a recurring **agent-lane job**
(`doctor_tick` job type on `claude_inproc`), 6–12 h cadence minted by a
`scheduler.py` `Cadence` row (host-agnostic, same idiom as
`health_digest`/`dream_agent`) plus on-demand dispatch when Layer 2
escalates a condition past its budget.
Rules notice; the doctor *reasons across signals* — the one thing
deliberately absent from the detection path. This is the folded home of
fleet-watching **T3** (`fleet_review`) and the health-watchdog residual
"periodic ops agent that auto-gathers status and has an LLM judge
reasonability".

Per tick:

1. **Gather** from the one health surface: `health_checks.py` outputs,
   open alerts (+ true totals), the claim-registry report, per-host
   `worker_logs` err/warn, `llm_call_log` rates, scheduler-lease
   staleness, host telemetry when T0 ships. No ssh fan-out; it reads
   what Layers 1–2 already publish.
2. **Classify by ratio, not count** (the codified `/whatneedsdoing`
   judgment): *broken pass* (≈100 % failure, zero outcome-table writes —
   P0) vs *noisy-but-working* (errors alongside successes — real bug,
   not an outage) vs *baseline noise* (green).
3. **Diagnose** with culprit-localization walks along each pipeline
   (template: `health_digest._diagnose_embed_pipeline` — materializer
   minting? jobs claimable? claims succeeding?), generalized per lane.
4. **Act only through existing rails**, per an autonomy dial (same shape
   as `precis.fixer`'s report/ship/full):
   - `report` (start here): author the daily report (see the reporting
     spine below), annotate/file gripes with the diagnosis, dedup
     against open ones, propose threshold/slot changes as gripes.
   - `heal`: execute Layer-2 whitelisted actions only (via the vetted
     action catalog — never raw Bash), each through `bounded_heal`.
   - `draft`: the doctor still only files gripes; `backlog_groom` turns
     them into `fix_gripe` todos → dispatch → `claude -p` branch rail
     (= Rung 1 below). Enabling `backlog_groom` (default-OFF,
     `PRECIS_BACKLOG_GROOM_ENABLED`) for doctor-filed gripes is an
     explicit prerequisite of this dial.
5. **Envelope**: target posture is read-mostly + gripe-only writes — a
   combination `workers/envelope.py` cannot express today (`agent_ro`
   comes only with `write:none`, which also tool-denies `put`;
   `write:scoped` stays `agent_rw` at the DB per its own docstring).
   **Prerequisite: a small envelope extension** (e.g. a `write:gripe`
   value — DB stays `agent_rw`, tool tier denies `Bash`/`Edit` and every
   precis mutating verb except `put` for gripes), closing part of the
   tier-2 enforcement gap gr179501. Until it lands, the doctor at
   `report` runs the same posture as the deep reviewer
   (`review.py::_REVIEWER_DISALLOWED_TOOLS` deny list at the permissive
   tier).

Evidence-base prerequisite: `quest_tick` / `catpath_explore` (and
doctor's own ticks) persist `meta.transcript` like `plan_tick` does —
today they discard the stream, a confusion-mining blind spot.

### Layer 4 — the autonomy ladder (how far the doctor's `draft` goes)

(Absorbed from health-watchdog phases 4–5; runs on the container
substrate; injection safety gr179498 is a Rung-1 prerequisite.)

| Rung | Behavior | Prod risk |
|---|---|---|
| 0 (today) | condition-linked gripe → human fixes | none |
| **1 — auto-draft, human-ship** (recommended start) | unattended reproduce (red test) → `coder` to green gate → `reviewer` sign-off → ready-to-`/go` branch, ping | none (no autonomous deploy) |
| 2 — auto-ship a whitelisted narrow class | config-flag flip, doc/typo, dep bump: ship + deploy unattended behind post-deploy verify + auto-rollback (re-run the very SQL check that found the problem) | bounded to the whitelist |
| 3 — widen the whitelist | each class earns Rung 2 | grows deliberately |

Hard limits (stated honestly): the fixer can't fix its own substrate
(fleet death / broken deploy / DB-down stay human — the dead-man's-switch
class); prod-state-dependent bugs won't reproduce in a sandbox, so
reproduce-first correctly refuses and escalates; per-gripe attempt cap +
global kill switch + bounded per-fix token budget.

### The reporting spine (alerts & reports fold here)

Detection stays deterministic; **human-facing reporting funnels through
one channel**, with the doctor as its author and a template as its
fallback:

- **Alerts are machine state, not a report.** `kind='alert'` stays the
  internal dedup'd condition table (raise/auto-resolve unchanged, never
  age-out). Humans meet alerts through the report and `/alerts`, not as
  a stream.
- **Criticals page immediately** — nursery → `notify_critical_alert` →
  ops channel, unchanged and LLM-free. Requires
  `PRECIS_OPS_ALERT_TARGET` set on system workers.
- **One periodic report, doctor-authored.** The doctor's `report` output
  replaces health_digest's templated push as the primary daily/degraded
  digest: classification, diagnosis, what was healed (claim-registry +
  bounded_heal forensics), what needs a human. **The deterministic
  templated digest remains as the fallback and the all-green heartbeat**
  — the digest must still send when the LLM/fleet is down (design law),
  and the daily all-green push stays the internal dead-man proof
  alongside the external ping.
- **Brief lane**: the morning brief cast (`reading/briefing_cast.py`)
  gets one health line sourced from the latest doctor report — an
  addition to the ops digest, not a replacement.
- **`/status`** stays the live panel, reading the same registries; add
  the claims-vs-liveness panel (handoff Part C: claims held vs owner
  liveness per host, so "3/4 deepseek slots held by dead PIDs" is one
  glance, not manual SQL).
- **`/whatneedsdoing`** step 5 (prod system-health SQL) collapses to
  "read the latest doctor report", keeping raw SQL as the fallback
  recipe.

## What gets absorbed / deleted (the simplify list)

- Slot-hold "TTL-only, 1 h" recovery → epoch arm (seconds after any
  deploy kill).
- Zombie agentlogs (no recovery today) → epoch-arm finalize.
- `_reap_stale_dft_containers`, `_reap_dead_node_orphans`, generic
  stuck-job sweep → claim-registry rows (behavior preserved, declaration
  unified).
- Five liveness predicates → two (epoch primary, TTL fallback) + Layer-0
  advisory locks; wall-clock tag age survives only as the backstop arm
  for claims that can't carry identity.
- Unpark's inline counters → shared `bounded_heal`.
- `embed-lane-stalled` hardcoding → per-lane probe rows.
- nursery/health_digest duplicate check shapes → one registry, two
  schedulers.
- health_digest's push as primary report → doctor-authored report with
  the template as fallback.
- The 6-restarts-per-deploy multiplier → one restart per unit per run.
- Four backlog files (health-watchdog, fleet-watching-agent, two nursery
  items) → this doc.

## What deliberately stays separate (independence is load-bearing)

- **Heartbeat** — must never depend on the claim machinery it is used to
  judge (its own docstring); stays a self-throttled pass + daemon thread.
- **Dead-man's switch** — must survive the doctor, the DB, and the
  fleet. The doctor is *inside* the blast radius; the deadman stays
  outside. Same for the health-watchdog residual **out-of-band
  DB-liveness watcher** (the 2026-07-05 ~8 h outage ran unalerted
  because every alerting path is DB-backed): external SELECT 1 from a
  non-fleet host → Discord on failure.
- **Detectors stay SQL-only, zero-LLM** — the doctor consumes their
  output and must never be in the path that produces it.
- **Real-time paging** stays nursery's, template-only.

## Deferred (parked here so the deleted files lose nothing)

- **T0 telemetry**: `host_samples` table (5-min self-reported rows:
  load, mem, swap, gpu, daemons-alive jsonb, extras; 14-day retention) +
  stamp `claimed_at`/`completed_at`/`wall_seconds` into every job's meta
  (today only plan_tick). Cheap, feeds the doctor, the per-lane probes,
  and any future governor — ship when a Layer-2 row first needs it.
- **T2 governor** (deterministic slot-based load balancing; absorbs
  materialize.py hysteresis; per-host utilization composite, pg host
  excluded-alert-only; backpressure counts PENDING not just leased):
  load management, not healing — its own decision later. Slot-cost v0
  from the 2026-08-10 watch: NEB=8 (GPU-exclusive), plan_tick=3,
  embed=2, fetch/classify=1; host budgets melchior 12, spark 1 GPU + 4,
  castor 4 llm-slots + 2, caspar 1 light, balthazar suspended (wired
  flap). Re-fit from T0. Open: new pass vs materialize rewrite; castor
  demand routing (bandwidth-bound → big-tier only).
- **Prod-ops disposition** of the 297 orphan + 540 stuck-doable todos
  surfaced by the alert-triage work (d4b0d354): substrate-2 ops work,
  not code; the deployed aggregate gripe is the hand-off surface.

## Safety constitution (each law has a scar behind it)

1. Probe outcomes (`STATUS:*` tags, derived-table writes), never
   process-exists or `/readyz` — both lied for 2 days (embedder wedge).
2. Alarm on backlog-not-draining, never on mere quiet (idle-aware).
3. Re-verify liveness immediately before any destructive act
   (`reaper-liveness-race.md`); epoch arm re-checks inside the
   transaction.
4. Never auto-resolve an alert on age — it masks a live backlog and
   resets the staleness signal (the refuted "age-out" design).
5. Restart-style heals are capped at 1 then escalate — kicking a wedge
   back into the same hang is the documented anti-pattern.
6. The doctor cannot fix its own substrate: fleet death, broken deploy,
   DB-down stay human (deadman class).
7. Doctor inputs (gripe text, log lines, transcripts) are untrusted
   data, never instructions — `untrusted-input-injection-scan.md`
   (gr179498) is a prerequisite for the `draft` dial.
8. Spend: doctor ticks ride existing budget gates; local-first tier.
9. The report must send when the LLM is down — template fallback,
   always.

## Sequencing (independently shippable slices)

This doc is the architecture umbrella, deliberately not TEMPLATE-shaped.
**Each slice is minted as its own TEMPLATE.md-shaped backlog item at
start-of-work** (motivation, scope, acceptance criteria, blast radius);
this doc stays the cross-slice record and is trimmed as slices ship.

1. **Claim registry + epoch on slot holds & agentlogs** (handoff Part A)
   — ✅ SHIPPED 2026-08-12: `precis.liveness` (the extracted shared
   predicate — explicit `(boot_id, host, process)` params, callers keep
   their storage-key mapping), migration 0123 (`holder_*` columns),
   identity stamped at `insert_slot_hold` + `open_log` (`meta.worker`),
   `workers/reaper.py` declarative epoch-arm pass in the sweeper
   (slot holds: delete + capped refund; agentlogs: finalize
   `status='aborted'`), 10-min age floor, in-transaction re-verify,
   uniform grep line. Job leases untouched (in-claim machinery).
2. **Graceful drain + single-restart deploy** (handoff Part B) —
   ✅ SHIPPED 2026-08-12: `precis.liveness.request_drain/drain_requested`
   (process-wide drain flag, flipped by the worker's SIGTERM/SIGINT
   handler), polled between SSE chunks (`ToolChatClient(abort_check=…)`)
   and between agent-loop turns (`run_tool_loop(abort_check=…)`) — an
   in-flight streamed LLM call aborts with its partial salvaged via the
   existing `StreamTimeout`/`paused` retry path; `service_unit` grew
   `service_unit_timeout_stop_sec` (systemd `TimeoutStopSec` / launchd
   `ExitTimeOut`), set to 60 s on the worker units; the install-notify
   handlers (worker/embedder/web) skip under `redeploy-precis.yml`, whose
   step-2 bounce is now THE one restart per code deploy (accepted
   exception: a unit-file-changing deploy double-bounces once via
   `service_unit`'s own reload — that reload is what makes launchd env
   changes take). Blocking (non-streamed) calls still ride out their
   timeout — the drain helps exactly the long streamed calls that used
   to hold units into SIGKILL. Verify post-deploy: zero
   `stop-sigterm timed out` in the journal.
3. **`bounded_heal` extraction + condition registry + first new rows** —
   ✅ core SHIPPED 2026-08-12: `workers/bounded_heal.py` (the unpark
   shape as a shared primitive — attempts + exponential cooldown + cap +
   terminal latch + one cap gripe; state in `app_settings` with a CAS
   bump so concurrent evaluators can't double-fire) and
   `workers/conditions.py` (registry evaluated on `health_digest`'s
   hourly lane, findings ride the existing alert-sync/router; rows:
   `pass-dead-on-host` — exact because every registered pass logs a
   `worker_logs` row every cycle, `rescue-pass-cadence`, `pass-wedged`,
   `llm-degraded`, `dead-generation-claims` — the claims-vs-liveness
   panel as a row). **restart-once** ships with the sudoers grant
   provisioned per deploy (`redeploy-precis.yml`, scoped to exactly one
   command per platform) but the heal arm is DARK until
   `PRECIS_RESTART_ONCE_ENABLED=1` — arming needs the deploy→deploy ssh
   mesh verified per host first. `PRECIS_OPS_ALERT_TARGET` turned out to
   be ALREADY SET (overlay `group_vars/all/precis_env.yml` →
   #systems-notifications; the doc's "dark today" was stale). Deferred
   from this slice (filed): fair sampling on capped detectors, the
   advisory-lock library helper, registering unpark/transient-reopen as
   bounded_heal rows (proven inline machinery — migrate, don't fork),
   fast-lane (nursery) wiring for future per-minute rows.
   **Acceptance case: gr204385** (classify handler silently dead in a
   live melchior worker since 2026-08-08, damming embed; unnoticed
   ~4 days) — `pass-dead-on-host` detects the class within one hourly
   window after its 4 h budget; the bounce stays manual until
   restart-once is armed.
4. **`doctor_tick` at `report`** + reporting-spine cutover (doctor
   authors the digest, template becomes fallback; brief lane;
   transcript persistence for agent job types).
5. **Dial up**: `heal`, then `draft` (Rung 1), then per-class Rung 2 —
   each class earns its way in. T0 telemetry lands whenever a row first
   needs it.

## Decisions (Reto, 2026-08-12)

- **Doctor's starting dial: pure `report`.** Heal/draft are earned
  later, per the sequencing slices.
- **Melchior SPOF: acceptable for now** (`agent-worker-despof.md`) —
  fine at the `report` dial; revisit before dialing up to `heal`.
- **`alert` write path: machinery-only.** Alerts stay probe-backed
  facts (fingerprinted, auto-open/auto-close when the probe flips);
  the doctor writes gripes — the judgment channel that tolerates
  uncertainty and rides the groom→fix_gripe rail. A recurring doctor
  finding graduates by *adding a condition-registry row*, never by the
  doctor raising alerts itself.
- **Rung 2 (auto-ship): deferred, ramp up eventually.** No class is
  admitted at launch; when ramping, each class needs the bar + the
  post-deploy verify/rollback mechanics
  (`deploy-verification-guards.md`) decided first.

## Open questions / decisions log

(added by the `ready` entrance-gate pass, 2026-08-12 — verified against
code in this worktree, not vibes)

- ~~blocker~~ **resolved 2026-08-12** — Not TEMPLATE.md-shaped: by design;
  the Sequencing section now states each slice is minted as its own
  template-shaped backlog item at start-of-work, this doc stays the
  umbrella.
- ~~blocker~~ **resolved 2026-08-12** — Layer 3 "5. Envelope" described a
  combination `envelope.py` cannot express (`agent_ro` only with
  `write:none`, which also tool-denies `put`; `write:scoped` = `agent_rw`
  at the DB). Spec rewritten: a small envelope extension (`write:gripe`)
  is a named prerequisite (part of the gr179501 tier-2 gap); until it
  lands the `report`-dial doctor runs the reviewer deny-list posture
  (`review.py::_REVIEWER_DISALLOWED_TOOLS`) at the permissive tier.
- ~~blocker~~ **resolved 2026-08-12** (gripe 204366) — dangling `§A`/`§D`
  citations at this doc: repointed to "Layer 2"; `health_digest.py`'s
  pervasive internal §-shorthand kept, with a module-docstring legend
  naming the folded `health-watchdog.md` (git history) as its source.
  (`cluster-scheduling.md`'s §-letters are that doc's own convention —
  untouched.)
- advisory — No `## Open questions / decisions log` heading existed before
  this edit (the doc instead had `## Decisions (Reto, 2026-08-12)`, covering
  resolved decisions only) — template-driven tooling that looks for the
  literal heading would have found nothing to append to.
- advisory — Layer 1's shared-predicate extraction
  (`src/precis/workers/executors/_common.py::_reclaim_reason`) is mechanically
  feasible (it already takes a generic `meta: dict[str, Any]`), but its
  `lease_boot_id`/`lease_process`/`lease_host` meta-key names are hardcoded
  literals — reusing it for `resource_slot_holds` columns and agentlog
  `meta` needs an interface decision (param names vs. a mapping) the spec
  doesn't state.
- advisory — Layer 1's "one declarative reaper phase" is a real,
  non-trivial refactor of ~6 bespoke phase functions in
  `src/precis/workers/sweeper.py` (`_reap_stale_dft_containers`,
  `_reap_dead_node_orphans`, `_run_unpark_pass`, the STATUS-running timeout
  sweep, …), each with documented special-case exclusions (`coordinator`
  excluded from `reclaim_stale_running`; `ssh_node` excluded from the
  generic sweep) that a declarative row list will have to reproduce
  faithfully — plausible, matches `model: opus`, but worth flagging before
  slice 1 starts.
- advisory — Part B's "SIGTERM aborts the in-flight LLM stream between SSE
  chunks" is finer-grained than what exists: `src/precis/cli/worker.py::
  _install_signal_handlers` today only "finishes the batch" on SIGTERM. The
  spec doesn't name which LLM-call code path needs the mid-stream abort
  hook.
- ~~advisory~~ **resolved 2026-08-12** — `doctor_tick` cadence trigger now
  named in Layer 3: a `scheduler.py` `Cadence` row, same idiom as
  `health_digest`/`dream_agent`.
- ~~advisory~~ **resolved 2026-08-12** — `draft` dial clarified in Layer 3:
  the doctor only ever files gripes; `backlog_groom` (default-OFF) minting
  the `fix_gripe` todos is an explicit prerequisite of that dial.

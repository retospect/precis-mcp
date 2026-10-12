# monitors that go quiet

## Resume

- **Pillar:** platform
- **Next:** Alert delivery is built (branch awaiting the orchestrator's gate); once it lands, deploy playbook 08 to the `monitoring` group and 18 to every node, then confirm a rule reaches the ops channel. Then [ranked work](#do-next).
- **Blocked by:** Waits are in [the latest handoff](#thread-context).
- **Unblocks:** Alerts that reach the user when shared infrastructure fails.
- **Acceptance:** Prometheus on the monitoring node reports an Alertmanager and loaded rules (`/api/v1/alertmanagers`, `/api/v1/rules`), and a test alert posted to the receiver shows in the ops channel.
- **Worktree:** `monitors-that-go-quiet`
- **Builds:** Not estimated here; use the owning item's current slice estimate.
- **Detail:** [Ranked work](#do-next) · [Horizon](#horizon) · [Coordination map](INDEX.md).

## Thread context

**Status:** ends when main always has a verdict and every fleet alert is
addressable by failure id and host identity, so silence means healthy. Four
things landed 2026-09-30: main-ci-status no longer announces either conclusion
off a cached listing; a main push is gated against the delta since the last sha
with a real shard verdict; qland runs ruff+mypy before it merges; and a host
dark past log retention still pages, because the detector no longer bounds
itself to a table the sweeper prunes. Two more landed the same day: a CLI that
exited 0 while printing a refusal now exits 3 on stderr, and the fix_gripe lane
stopped reporting deliveries it had not made — a fix branch counts as delivered
only once `git ls-remote` finds it on the repo's real upstream, which the lane
had never once reached. On 2026-10-02 the NAS probe stopped attesting only its
own interpreter: each NAS-touching process now attests itself (built,
awaiting deploy). What remains is one signal that lies by omission, a worker
host no detector can see, and (found 2026-10-04) that no Prometheus alert rule
was evaluated anywhere, so every one of them was silent (built 2026-10-11: rules, Alertmanager and a loopback receiver into the ops channel; awaiting deploy). The last held decision closed 2026-09-30: `ship --quick`
warns when main's last shard verdict is 24h old and refuses at 48h, on Reto's
"a day or two", and never refuses on an age it could not look up. The container host's forensics were
answered before the 30-day prune took them; what they turned up — an
unattributable identity claiming and failing prod jobs — is bigger than this
thread and is flagged on the Horizon for an owner.
**Last reviewed:** 2026-10-04 (reopened for the alert-delivery job; built 2026-10-11, awaiting deploy); 2026-10-03 (round-3 dogfood PASS: diagnose job 464662 on 929107f3 ledgered cost_usd 0.21, 8 turns, input_tokens set; td464074 closed); 2026-10-03 (round-2 dogfood: diagnose ledger nulls fixed); 2026-10-03 (doctor stops filing gripe/alert-tracked asks as Reto todos); 2026-10-02 (lane-close prod writes done); 2026-10-02 (fix_gripe lane closed by ruling, Parked emptied); 2026-10-02 (NAS attestation built on Reto's option-1 ruling); 2026-10-02 (stranded-branch work finished and deployed); 2026-09-30 (pillar review same day added four orphan
gripes and the fix_gripe self-repair cluster as one Parked entry; pruned
gr346534, soft-deleted)
**Worktree:** `monitors-that-go-quiet`
**Resume (2026-10-11, alert delivery built):**
- Built: `precis alertmanager-receiver` (loopback webhook, queues one ops
  message per Alertmanager group), the Linux path of the `monitoring` role
  (Prometheus config with `rule_files` + `alerting`, Alertmanager, receiver
  unit, end-to-end API checks), and the `monitoring` inventory group in
  playbook 08. Nothing is deployed. Activation needs the orchestrator: add a
  Linux node to `monitoring` in the private inventory, put
  `PRECIS_OPS_ALERT_TARGET` in `precis_shared_env`, run playbooks 18 then 08
  there (08 keeps a backup of the hand-edited `prometheus.yml`; diff its scrape
  jobs first, because a new always-down target fires NodeDown).
- Accepted trade-off (Reto 2026-10-04): these alerts are silent while Postgres
  or asa_bot is down, PostgresDown included.
- Done and verified on prod earlier: the doctor filer fix (tick 464516), the
  diagnose_gripe cost ledger (job 464662).
- Waiting on others: the review session's resend of dedupe items 1 and 2
  (Do-next 1); the orchestrator's round for the NFS-hang branch (Do-next 2),
  whose `*Hung`/`*Absent` rules join the rendered set when it lands.
- Later, no outside wait: the attributability-journal design note (Do-next 3),
  then Do-next 4 once it is ruled ready.

## Do next

1. **Stop filing tracked asks as Reto todos** (Reto 2026-10-03, this
   thread's top item). The doctor filed 217 of the 264 open
   `waiting-for:reto` todos (parent td347578); its prompt told it to list
   "a gripe you filed or annotated" as an ask. `convert_needs_a_human` now
   files a bullet only when no open gripe, alert or todo it names already
   tracks it (`_open_trackers` in `precis.workers.doctor_report`), and the
   prompt says so. Two more filer gaps from the review session's dedupe
   analysis are fixed in the same change. First, an ask minted before
   `doctor_ask_refs` existed is now matched on the handle in its title:
   td462461 was minted while td456667 was open. Second, a
   "No queryable surface" tool-gap bullet stays in the report as a
   non-ask instead of minting (td455178). Deployed in round 2
   (63301c5c, 13:49Z 2026-10-03). Dogfood PASS at 18:17Z. The first
   post-deploy tick was job 464516 (`doctor:2026-10-03/2`, 18:06Z,
   succeeded). Its Needs-a-human section is "None.", and it minted 0 todos
   under td347578. The report draft for that day (462892) has 0
   "(no ask: tracked by" lines. So the prompt change kept the tracked
   bullets out entirely, and the deterministic gate has not yet been
   exercised on prod. The first tick that renders a "tracked by" line
   proves the gate. Still owed: items
   (1) and (2) of that analysis arrived cut off. Get them resent and fix
   them at source. The dedupe agent closes existing duplicates; this
   thread does not. Of its two hand-offs, td455178 and
   td345821 are both closed (caspar runs no daemons by design).
2. **The /mnt/cluster NFS-hang alert** (from local-compute, 2026-10-02). The
   share has hung on every client since 2026-09-30, and the only rule
   (`avail_bytes == 0`) cannot fire on a hang. Branch
   `worktree-agent-a8ce270a39be79437` @ `2443c04d` adds `*Hung`
   (`node_filesystem_device_error == 1`) and `*Absent` (an up node with no
   avail series) rules for /mnt/cluster and the NAS. It is with the
   orchestrator; it did not make round 2 (not in origin/prod
   63301c5c), so it rides round 3 or later. Expect it to page at once on the known
   hang until local-compute-6's recovery. The NAS absence rule renders only
   once a `nas_mount_hosts` group exists, because autofs makes an idle node's
   missing series normal.
3. **backlog/unnamed-container-host-wrote-211k-worker-logs.md** — its ask 1,
   the attributability journal: one event when a non-fleet identity starts
   writing to prod, carrying whatever provenance exists. The investigation
   half is CLOSED as of 2026-09-30 (answers in the item, read before the prune
   took them), so what is left is the monitor. Second: it is the only open
   code work here that is mine to start, but nothing is specced yet and the
   thing it would watch is not currently costing anything.
4. **backlog/b2-offsite-sync-dark-alert.md** (filed 2026-10-03 for the
   orchestrator; draft, do not build yet). The nightly B2 sync failed on
   every run for 7 weeks into a log nobody reads. The fix is a
   `health_digest` check on the log, because the DB node has no tick of
   its own. It must be timeout-guarded against the NFS hang in item 2.
5. **backlog/alert-sources-without-a-tracker.md** (filed 2026-10-10 from
   gr472019 and gr476100; the router tracker binding shipped, what is left
   is the OTS batch 7 re-stamp). It ranks below 1–4 because the alerts still
   fire; what is missing is the tracker binding, so nothing goes silent.
6. **backlog/mcp-call-latency-alerting.md** (gr479849, ruled 2026-10-12).
   Tool-call p50/p95 from the durable `tool_calls` ledger plus a
   health-digest budget check; today only a per-process ring exists, so a
   slow MCP goes unnoticed.

## Horizon

1. **A guard on whether an ephemeral identity may claim jobs at all** —
   **filed 2026-09-30** at Reto's direction, so it now has an
   owner and leaves this thread. It is prod-work integrity, not monitoring:
   the container claimed 53 nursery jobs and failed all 53, plus 16 axis jobs
   likewise, under an identity nobody can contact, alert on, or trace once
   `worker_logs` prunes (around 2026-10-09, after which the evidence is gone).
   Ruled 2026-10-11 (Reto): such identities may keep claiming; record an
   attribution event per claim instead of refusing.
   Left on the Horizon only as a pointer — Do-next 3 is the narrow read-only
   slice of it and is still this thread's.
2. **backlog/alert-failure-id-registry.md** — the registry, `/rules`
   catalogue and idle-aware health panel shipped 2026-10-09; "did host-dark
   fire, for which host" is addressable by id. `acked_until` TTL shipped
   (web ack/unack, readers filter it). `/status`'s Health tab renders the
   `health_panel()` object and producers bind `RULES_BY_ID[...].rule`
   (2026-10-12). Left, per the file: an agent verb to ack.
3. **backlog/self-healing-spine.md** — Layer 1 owns worker identity, Layer 2
   the condition registry; Do-next 3 exists because a host has no
   durable identity separating "ephemeral by design" from "vanished", so
   attributability-by-container is a slice here. Last: largest, no
   independently shippable piece touching this thread.
4. Unify `/alerts`, `/gripes` and `/needs-you` into one triage surface:
   a design question in Reto's queue as td479394, not a defect.
5. **backlog/doctor-report-and-alert-channel-quality.md** — doctor and alert
   output quality, agent-lane container env, worker_logs ts index; the
   channel the detectors above report through. Platform pass 2026-10-02.

## Parked

- (none)

## No action needed

- **the fix_gripe self-repair lane** — closed
  by Reto's ruling 2026-10-02 (td460703, "Drop the lane, we run locally
  session here"): lane OFF. It never delivered; every job skipped at the dry
  run because melchior's fix checkout holds no push credential. Switch: prod
  `service_config` row melchior/`backlog_groom` set prio 5 to 0 at
  2026-10-02 22:04Z, so the groomer mints no `fix_gripe` todos. Re-arm only
  on Reto's say: `precis service prio melchior backlog_groom 5`. Code and the
  job type stay. `diagnose_scan` was switched off too (melchior prio 5 to 0, 2026-10-02 22:37Z, Reto via review item monitors-that-go-quiet-2; re-arm `precis service prio melchior diagnose_scan 5`). Threaded sessions now fix their
  own gripes. Any gripe from the 09-30 reset still needs "is the fix already
  in main?" asked before it is ranked. Residue closed on prod 2026-10-02
  about 23:10Z: td461210 `won't-do` (its 7 leaves' gripes were all fixed on main;
  evidence in its body), the 35 open groomer todos under td375465
  `won't-do`, jobs 462123–462125 already `cancelled`, and no fix_gripe job
  queued or claimed.

- **the fix_gripe skip path** — observed on prod 2026-09-30, so the lane's
  inertness is no longer a code-reading claim. Four real runs
  (job:458512, 458575, 458576, 458577) each ended in 0.9s on melchior running
  `8.35.1@824a2734` with the exact refusal the preflight is for
  (`could not read Username for 'https://github.com': terminal prompts
  disabled`), no agent spawned. The budget half holds too, and by construction
  rather than luck: the outcome lands as job status `cancelled`, and
  `sweeper.py` excludes the cancelled case from `bubble_job_failure`, so no
  failure bubble reaches the parent — td458571 is still `STATUS:open`, not
  parked. Nothing here to do.
- **the cancelled-run verdict hole** — closed 2026-09-30 by
  `scripts/last-gated-main-sha` + check.yml's main-push range. The range can
  only widen, so the failure direction is over-gating; a lookup that cannot
  answer gates fully.
- **host-dark ageing out with worker_logs retention** — closed 2026-09-30.
  `_detect_host_dark` no longer reads `worker_logs` at all; a decommissioned
  host is excluded by an explicit `meta.retired` stamp
  (`precis heartbeat --retire <host>`, which refuses a host still beating
  because the heartbeat UPSERT would clear the marker on its next beat).
  `HOST_DARK_LOOKBACK_DAYS` is gone. The durable pin is structural, not a
  number: a test asserts the detector's code never mentions `worker_logs`.
- **qland as the gate bypass** — closed 2026-09-30: `scripts/ship --quick`
  runs ruff + mypy + import contracts before the squash-merge (no pytest, no
  test DB, no fleet gate slot), and the qland skill now says to run the tests
  you added. CLAUDE.md, the qland skill and the qgo skill were all telling
  agents qland ran nothing, so they changed in the same commit.

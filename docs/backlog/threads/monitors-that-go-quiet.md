# monitors that go quiet

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
own interpreter: each NAS-touching process now attests itself (gr248866, built,
awaiting deploy). What remains is one signal that lies by omission, a worker
host no detector can see. The last held decision closed 2026-09-30: `ship --quick`
warns when main's last shard verdict is 24h old and refuses at 48h, on Reto's
"a day or two", and never refuses on an age it could not look up. The container host's forensics were
answered before the 30-day prune took them; what they turned up — an
unattributable identity claiming and failing prod jobs — is bigger than this
thread and is flagged on the Horizon for an owner.
**Last reviewed:** 2026-10-02 (lane-close prod writes done; gr462731 filed); 2026-10-02 (fix_gripe lane closed by ruling, Parked emptied); 2026-10-02 (gr248866 built on Reto's option-1 ruling; gr245505 verified on prod); 2026-10-02 (gr458459/gr452203/gr452084 found shipped by siblings and verified on prod; gr454480 fixed; gr248866 adopted); 2026-10-02 (stranded-branch work finished and deployed; gr458899 closed on prod); 2026-09-30 (pillar review same day added four orphan
gripes and the fix_gripe self-repair cluster as one Parked entry; pruned
gr346534, soft-deleted)
**Worktree:** `monitors-that-go-quiet`

## Do next

1. **Verify gr248866 after deploy.** Each long-running NAS-touching process
   now lists `/opt/nas` on boot and every
   `PRECIS_NAS_ATTEST_INTERVAL_SECONDS` (600) and writes
   `host_heartbeat.meta.nas_ok_by_process[<process>]` with its own resolved
   `exe`. The writers are web (lifespan), `precis serve` (`main`), and the
   worker/heartbeat (`_collect_and_upsert`). `_detect_nas_denied` alerts per
   (host, process) on a fresh (30 min) `ok=false` entry. The legacy top-level
   `nas_ok` check remains only for rows with no attestations. Check: after
   the deploy, every Mac's row carries `precis-web`/worker keys with recent
   `ts` and `ok=true`. Three known gaps are recorded on the gripe (comment 7)
   and in `precis.workers.heartbeat`'s docstring: one-shot timer
   interpreters never attest, caspar has no row to land on, and stdio serves
   share one key. The remedy text follows `launched_by`: the macOS
   responsible process, read via `responsibility_get_pid_responsible_for_pid`.
   That is launchd for the python, ssh for the Remote Login setting, terminal
   for the `.app`, and container processes don't attest. Reto's open call on
   the reconcile-sweep design (gripe_180306) is td461151.
2. **The /mnt/cluster NFS-hang alert** (from local-compute, 2026-10-02). The
   share has hung on every client since 2026-09-30, and the only rule
   (`avail_bytes == 0`) cannot fire on a hang. Branch
   `worktree-agent-a8ce270a39be79437` @ `2443c04d` adds `*Hung`
   (`node_filesystem_device_error == 1`) and `*Absent` (an up node with no
   avail series) rules for /mnt/cluster and the NAS. It is with the
   orchestrator to land in round 2. Expect it to page at once on the known
   hang until local-compute-6's recovery. The NAS absence rule renders only
   once a `nas_mount_hosts` group exists, because autofs makes an idle node's
   missing series normal.
3. **diagnose_gripe's spend is now ledgered** (round 2). It writes `cost_usd`
   to the job meta and one `llm_call_log` row (`source='diagnose_gripe'`).
   Reto turned the automatic `diagnose_scan` off 2026-10-02, so rows now come
   only from hand-submitted jobs. fix_gripe has the same gap, but its lane is
   off.
4. **backlog/unnamed-container-host-wrote-211k-worker-logs.md** — its ask 1,
   the attributability journal: one event when a non-fleet identity starts
   writing to prod, carrying whatever provenance exists. The investigation
   half is CLOSED as of 2026-09-30 (answers in the item, read before the prune
   took them), so what is left is the monitor. Second: it is the only open
   code work here that is mine to start, but nothing is specced yet and the
   thing it would watch is not currently costing anything.

## Horizon

1. **A guard on whether an ephemeral identity may claim jobs at all** —
   **filed 2026-09-30 as gr458458** at Reto's direction, so it now has an
   owner and leaves this thread. It is prod-work integrity, not monitoring:
   the container claimed 53 nursery jobs and failed all 53, plus 16 axis jobs
   likewise, under an identity nobody can contact, alert on, or trace once
   `worker_logs` prunes (around 2026-10-09, after which the evidence is gone).
   Left on the Horizon only as a pointer — Do-next 5 is the narrow read-only
   slice of it and is still this thread's.
2. **backlog/alert-failure-id-registry.md** — status ready; stable failure
   ids make "did host-dark fire, for which host" addressable instead of SQL
   archaeology. Leverage over Do-next 5 and shippable now.
3. **backlog/self-healing-spine.md** — Layer 1 owns worker identity, Layer 2
   the condition registry; Do-next 5 exists because a host has no
   durable identity separating "ephemeral by design" from "vanished", so
   attributability-by-container is a slice here. Last: largest, no
   independently shippable piece touching this thread.
4. **gr415963** — discuss: unify `/alerts`, `/gripes` and `/needs-you`
   into one triage surface (view-only merge, or leave as three). A design
   question, not a defect; no urgency driving it.
5. **backlog/doctor-report-and-alert-channel-quality.md** — doctor and alert
   output quality, agent-lane container env, worker_logs ts index; the
   channel the detectors above report through. Platform pass 2026-10-02.

## Parked

- (none)

## No action needed

- **the fix_gripe self-repair lane** (gr458326, gr452384, gr456240) — closed
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
  queued or claimed. The guard-prod-psql hook that stopped the pane on the
  ruled switch writes is gr462731.

- **gr245505** — verified on prod 2026-10-02. After the 81154bc0 deploy
  (cut 2026-10-01 23:14Z), the 2 structural reviews took 4 and 11 turns
  (`llm_call_log`, `source='review:structural'`), and no
  `review:tool-starved:structural` alert has fired. In the 7 days before
  the deploy, 7 of 29 runs were single-turn starves. The last pre-deploy
  alert (459405) read `precis=pending`, but its text head was a usage-limit
  message, so the usage cap may have caused that one. Tool-call counts are
  not persisted; `turns_used` is the proxy.

- **gr454480** — fixed 2026-10-02, pending close. A fix agent that makes no
  commit because the defect is already gone now ends on an
  `ALREADY FIXED: <evidence>` line; `fix_gripe.run` returns `already_fixed`,
  and the executor marks the job succeeded and moves the gripe to
  `in_review` with the evidence in a comment — no failure bubble, no
  reopen. The groomer only mints for `STATUS:open` gripes, so this cannot
  loop the way the stranded branch did. The line is honoured only on a
  clean finish (no `terminal_reason`), so an agent cut off by `max_turns`
  still fails.
- **gr458459, gr452203, gr452084** — shipped by sibling sessions
  2026-10-01 and deployed (in `origin/prod`). backlog-lint gates on
  front-matter `status:` and names its evidence (1977b48b8); doctor asks
  dedup on the gripe/alert/commit handles a bullet names (52a6ed3ed) — prod
  2026-10-02: 50 open asks, down from 161, with `seen_count=2` on the first
  re-asks; the kind-shrinkage detector has its recency anchor and registry
  cross-check (`_detect_kind_shrinkage`) — prod: 0 kind-shrinkage alerts
  since 2026-10-01.

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
- **gr458317** — fixed 2026-09-30, pending close. `precis tools` sent an
  `[error:…]` refusal to stdout at exit 0; it now goes to stderr at exit 3,
  kept distinct from 1 (the CLI crashed) so a caller can tell "the verb said
  no" from "the tool is broken". The gripe's one unaudited risk is clear:
  nothing in the tree calls `precis tools` or `prod-precis` programmatically.
  Fixing it turned up the same defect in `precis eval` — right exit code,
  wrong stream, and no check at all for a rendered refusal string, so that
  case also exited 0 — so the contract now lives in
  `precis.cli._common.is_refusal` / `REFUSAL_EXIT` and both commands share it.
- **gr456236** — fixed 2026-09-30, pending close: the staleness guard now
  runs on the green verdict too and prints even under `--for-hook`. Landed
  from the gripe's own diagnosis, not from the auto-fix lane's branch
  (`gripe_456236` was gone from origin by the time it was reviewed).
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

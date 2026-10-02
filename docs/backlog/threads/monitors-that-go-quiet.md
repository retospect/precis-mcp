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
**Last reviewed:** 2026-10-02 (gr248866 built on Reto's option-1 ruling; gr245505 verified on prod); 2026-10-02 (gr458459/gr452203/gr452084 found shipped by siblings and verified on prod; gr454480 fixed; gr248866 adopted); 2026-10-02 (stranded-branch work finished and deployed; gr458899 closed on prod); 2026-09-30 (pillar review same day added four orphan
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
   share one key. Reto's open call on the reconcile-sweep design
   (gripe_180306) is td461151.
2. **backlog/unnamed-container-host-wrote-211k-worker-logs.md** — its ask 1,
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
   Left on the Horizon only as a pointer — Do-next 2 is the narrow read-only
   slice of it and is still this thread's.
2. **backlog/alert-failure-id-registry.md** — status ready; stable failure
   ids make "did host-dark fire, for which host" addressable instead of SQL
   archaeology. Leverage over Do-next 2 and shippable now.
3. **backlog/self-healing-spine.md** — Layer 1 owns worker identity, Layer 2
   the condition registry; Do-next 2 exists because a host has no
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

- **the fix_gripe self-repair lane** (**gr458326**, **gr452384**,
  **gr456240**) — failing at every stage: a clean exit with
  no commits counted as a failure (gr454480, fixed 2026-10-02 — see No
  action needed), "pushed to origin" claimed and never
  verified (43 branches stranded on the worker node, never reaching
  origin), a hard `max_turns=20` ceiling with no escalation on complex
  fixes, and infra-class failures (API rate limits, container
  unavailability) consuming the same unpark-attempt budget as a real
  failed fix. Reto ruled 2026-09-30 (Do-next 1 above): **leave it not
  doing anything.** Superseded 2026-10-01 (td459082): **the lane pushes
  straight to main; downstream is the publish system** (check.yml on main,
  `origin/gated`). The code side is in: a successful run fetches the
  agent's branch into the host checkout and squash-lands it on current main
  with a non-force (fast-forward CAS) push, the `scripts/ship` protocol. Still
  inert until melchior's fix checkout holds a push credential — operator
  steps are on gr458326; until then every job skips at the dry run as before. gr456240's infra-failure classification landed 2026-10-01 from its stranded
  branch. Side effect to watch: the reset of the 39 parked gripes to
  `open` re-surfaced at least one already-fixed gripe as current
  (gr458087 — the STRtree fix it proposed is in `check_via_pad_keepout`
  and cites it; ewod-pcb re-measured 2026-09-30 and queued the close for
  Reto). Any gripe from that reset needs "is the fix already in main?"
  asked before it is ranked.

## No action needed

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

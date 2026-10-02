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
had never once reached. What remains is one signal that lies by omission (a
worker host no detector can see) and the tail of the stranded fix branches
(19 landed 2026-10-01; eight salvage items left, each owned by a thread). The last held decision closed 2026-09-30: `ship --quick`
warns when main's last shard verdict is 24h old and refuses at 48h, on Reto's
"a day or two", and never refuses on an age it could not look up. The container host's forensics were
answered before the 30-day prune took them; what they turned up — an
unattributable identity claiming and failing prod jobs — is bigger than this
thread and is flagged on the Horizon for an owner.
**Last reviewed:** 2026-10-02 (stranded-branch work finished and deployed; gr458899 closed on prod); 2026-09-30 (pillar review same day added four orphan
gripes and the fix_gripe self-repair cluster as one Parked entry; pruned
gr346534, soft-deleted)
**Worktree:** `monitors-that-go-quiet`

## Do next

1. **This thread's three stranded-branch salvage items** — gr452203
   (Do-next 4 below), gr454480 (Parked, the lane) and gr248866 (does a child
   process inherit the heartbeat's macOS TCC grant? if not, the probe is a
   false green). The stranded-branch work itself is finished and deployed
   (2026-10-01/02): 19 keepers landed incl. `gripe_182230` as migration 0175,
   all 43 node branches deleted, scratch clones gone. Each salvage branch's
   code is only in the bundle — location and per-item notes in
   **backlog/stranded-fix-gripe-branches.md**; the other five salvage items
   belong to other threads and are named there. Reto's open call on the
   reconcile-sweep design (gripe_180306) is td461151.
2. **backlog/unnamed-container-host-wrote-211k-worker-logs.md** — its ask 1,
   the attributability journal: one event when a non-fleet identity starts
   writing to prod, carrying whatever provenance exists. The investigation
   half is CLOSED as of 2026-09-30 (answers in the item, read before the prune
   took them), so what is left is the monitor. Second: it is the only open
   code work here that is mine to start, but nothing is specced yet and the
   thing it would watch is not currently costing anything.
3. **gr458459** — backlog-lint has told every session in the fleet to
   delete open specs for at least two days and the count is growing 2→5;
   4 of 5 current hits are false and the tool's own footer says so without
   gating the advice. An advisory with this a false-positive rate trains
   the fleet to ignore the channel the one genuine hit arrives on.
4. **gr452203** — doctor asks never dedup: 161 open `waiting-for:reto`
   todos, 161 unique keys, `seen_count=1` on every one, because the dedup
   key hashes the model's re-authored prose instead of the referenced
   gripe/alert handle. Rebuilds at ~20 rows/day without a fix.
   The stranded `gripe_452203` tried this and is salvage only: its dedup
   still misses the gripe's own example.
5. **gr452084** — the nursery kind-shrinkage detector fired 12 critical
   alerts on its first pass and 0 were real (stale-boot comparisons, a
   deliberately retired kind, env-gated kinds). Net-negative for this
   thread's own "unremarkable doctor report" goal until fixed.
   Only its defect 4 (the unbounded `kind_provider` table) landed
   2026-10-01, from the stranded branch; the false criticals are still open.
6. **Verify the structural reviewer gets its tools** (gr245505, closed). On
   10+ runs from 2026-09-19 it finished its one turn while
   `mcp init: precis=pending`, making zero tool calls. Two fixes deployed
   in 81154bc0. The first is the root fix: `claude -p` runs get
   `CLAUDE_CODE_MCP_STARTUP_WAIT_MS=30000`
   (`claude_agent._prepare_agent_env` and the container executor's env).
   It needs CLI 2.1.274 or later; melchior has 2.1.285. The second is a
   retry-once in `review.py`, the backstop. Check: the first structural
   review after that deploy makes more than 0 tool calls. A fresh
   `structural:tool-starved` alert reading `precis=pending` means the wait
   is not taking effect; reopen gr245505 with that alert's detail. The three
   duplicate Reto asks (td459083, td456036, td454135) are done.

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

## Parked

- **the fix_gripe self-repair lane** (**gr454480**, **gr458326**,
  **gr452384**, **gr456240**) — failing at every stage: a clean exit with
  no commits counted as a failure, "pushed to origin" claimed and never
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
  branch; gr454480's branch is salvage only — an already-fixed run reopens the
  gripe, so it can loop. Side effect to watch: the reset of the 39 parked gripes to
  `open` re-surfaced at least one already-fixed gripe as current
  (gr458087 — the STRtree fix it proposed is in `check_via_pad_keepout`
  and cites it; ewod-pcb re-measured 2026-09-30 and queued the close for
  Reto). Any gripe from that reset needs "is the fix already in main?"
  asked before it is ranked.

## No action needed

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

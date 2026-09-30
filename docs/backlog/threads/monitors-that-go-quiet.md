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
worker host no detector can see), one decision on whether a lane that cannot
deliver should keep running, and one held decision on how far
behind its verdict main may drift before a qland is refused. The container host's forensics were
answered before the 30-day prune took them; what they turned up — an
unattributable identity claiming and failing prod jobs — is bigger than this
thread and is flagged on the Horizon for an owner.
**Last reviewed:** 2026-09-30
**Worktree:** `monitors-that-go-quiet`

## Do next

1. **Decide what happens to the 43 stranded fix_gripe branches** — merge the
   good ones, or delete them. Reto 2026-09-30, on the lane itself: **leave it
   not doing anything.** That is settled, and the code already behaves that
   way — a `push --dry-run` before the agent is spawned turns an undeliverable
   worker into a skip costing one round trip, so the lane is inert without
   being disabled. What is left is the branches, and they are the perishable
   part: all 43 are from 2026-09-25 to 09-30 and sit 36 to 254 commits behind
   main, so they decay every day nobody looks. Triage, and the two things that
   triage did *not* establish, are in
   **backlog/stranded-fix-gripe-branches.md**.
   The 39 gripes the lane had parked at `in_review` behind branches that do
   not exist — every `in_review` gripe in the database — are reset to `open`
   as of 2026-09-30, each carrying a comment saying why.
2. **backlog/unnamed-container-host-wrote-211k-worker-logs.md** — its ask 1,
   the attributability journal: one event when a non-fleet identity starts
   writing to prod, carrying whatever provenance exists. The investigation
   half is CLOSED as of 2026-09-30 (answers in the item, read before the prune
   took them), so what is left is the monitor. Second: it is the only open
   code work here that is mine to start, but nothing is specced yet and the
   thing it would watch is not currently costing anything.
3. **backlog/main-stays-gated.md** — how far behind its last shard verdict main
   may drift before `scripts/ship --quick` refuses rather than warns. Reto
   2026-09-30: **a day or two**, pending confirmation against the number the
   deploy session holds. Both directions are written up in the item; with the
   number settled this is a few lines plus a test.

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

## Parked

- (none)

## No action needed

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
- **gr346534** — the post-merge-run gripe gr456236 cites; soft-deleted, do
  not chase.
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

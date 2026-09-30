# monitors that go quiet

**Status:** ends when main always has a verdict and every fleet alert is
addressable by failure id and host identity, so silence means healthy. Four
things landed 2026-09-30: main-ci-status no longer announces either conclusion
off a cached listing; a main push is gated against the delta since the last sha
with a real shard verdict; qland runs ruff+mypy before it merges; and a host
dark past log retention still pages, because the detector no longer bounds
itself to a table the sweeper prunes. What remains is one signal that lies by
omission (a worker host no detector can see), one that lies outright (a CLI
that exits 0 on an error), and one held decision on how far behind its verdict
main may drift before a qland is refused. The container host's forensics were
answered before the 30-day prune took them; what they turned up — an
unattributable identity claiming and failing prod jobs — is bigger than this
thread and is flagged on the Horizon for an owner.
**Last reviewed:** 2026-09-30
**Worktree:** `monitors-that-go-quiet`

## Do next

1. **gr458317** — `precis tools` prints an `[error:…]` payload to stdout and
   exits 0, so a caller gets a successful exit and an error string where data
   should be. Filed from hexfold-toolkit and handed here because `scripts/`
   has no thread owner and this is the thread's exact class. First: the fix is
   specified in the gripe, it is small, and the blast radius is the worst
   shape there is — `scripts/prod-precis` is the documented fallback for when
   the session MCP is dead, so it lies precisely when the caller has least
   other information. Its one open risk, whether an existing script depends on
   the current exit 0, is audited and clear: nothing in the tree calls
   `precis tools` or `prod-precis` programmatically (only prose and comments).
2. **backlog/unnamed-container-host-wrote-211k-worker-logs.md** — its ask 1,
   the attributability journal: one event when a non-fleet identity starts
   writing to prod, carrying whatever provenance exists. The investigation
   half is CLOSED as of 2026-09-30 (answers in the item, read before the prune
   took them), so what is left is the monitor. Below gr458317 because nothing
   is specced yet, though the finding it rests on is now evidence rather than
   suspicion.
3. **backlog/main-stays-gated.md** — one follow-on, and it is a question
   rather than a task: how far behind its last shard verdict main may drift
   before `scripts/ship --quick` refuses rather than warns. Both directions
   are written up in the item; it wants Reto's number, then it is a few lines.

## Horizon

1. **A guard on whether an ephemeral identity may claim jobs at all** — ask 2
   of the container item, and unfiled as its own thing because it may not
   belong here: it is prod-work integrity, not a monitor. Evidence from the
   2026-09-30 forensics: the container claimed 53 nursery jobs and failed all
   53, plus 16 axis jobs likewise, under an identity nobody can contact,
   alert on, or trace once `worker_logs` prunes. Needs an owner thread before
   it can be ranked — flagged here so it is not lost with the item whose
   investigation half just closed.
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

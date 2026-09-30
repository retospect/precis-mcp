# monitors that go quiet

**Status:** ends when main always has a verdict and every fleet alert is
addressable by failure id and host identity, so silence means healthy. The
main-verdict half is done as of 2026-09-30: main-ci-status no longer announces
either conclusion off a cached listing, a main push is gated against the delta
since the last sha with a real shard verdict, and qland runs ruff+mypy before
it merges. What remains is the two fleet signals that lie by omission — a
host-liveness detector whose evidence window empties itself, and a worker host
no detector can see — plus one held decision on how far behind its verdict
main may drift before a qland is refused.
**Last reviewed:** 2026-09-30
**Worktree:** `monitors-that-go-quiet`

## Do next

1. **backlog/unnamed-container-host-wrote-211k-worker-logs.md** — first
   only because its evidence expires: re-measured 2026-09-30, the host's
   `worker_logs` rows are down from 211K to 30.7K and its last row
   (2026-09-09) is pruned around **2026-10-09**, after which nothing records
   that it ran at all — it has no `host_heartbeat` row either. The design
   question (attributability for a non-fleet identity writing to prod) is not
   urgent; dumping the surviving rows somewhere durable is, and is cheap. Do
   that part first, then rank the rest normally.
2. **backlog/host-dark-ages-out-with-worker-logs-retention.md** — the
   host-dark lookback equals worker_logs retention, so a host dark longer
   than retention becomes permanently invisible: the alert ages out because
   the evidence was deleted. Prevents a broken host reading as
   decommissioned. Specced down to code 2026-09-30 (option 1, the explicit
   retire marker; the shape and a prod re-read that confirms nothing needs
   backfilling are in the item) — so it is ready to write, and sits here
   rather than first only because it is latent with no incident.
3. **backlog/main-stays-gated.md** — down to one follow-on and it is a
   question, not a task: how far behind its last shard verdict main may drift
   before `scripts/ship --quick` refuses rather than warns. Refusing too eagerly
   strands trees behind an unclaimed red; warning is what main already does and
   what let it reach 20-odd ungated commits. Last because the code is a few
   lines and the decision is Reto's — see the item for both directions.

## Horizon

1. **backlog/alert-failure-id-registry.md** — status ready; stable failure
   ids make "did host-dark fire, for which host" addressable instead of SQL
   archaeology. Leverage over Do-next 1–2 and shippable now.
2. **backlog/self-healing-spine.md** — Layer 1 owns worker identity, Layer 2
   the condition registry; Do-next 1 and 2 exist because a host has no
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
- **qland as the gate bypass** — closed 2026-09-30: `scripts/ship --quick`
  runs ruff + mypy + import contracts before the squash-merge (no pytest, no
  test DB, no fleet gate slot), and the qland skill now says to run the tests
  you added. CLAUDE.md, the qland skill and the qgo skill were all telling
  agents qland ran nothing, so they changed in the same commit.

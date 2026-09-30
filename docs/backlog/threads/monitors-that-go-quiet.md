# monitors that go quiet

**Status:** ends when main always has a verdict and every fleet alert is
addressable by failure id and host identity, so silence means healthy.
Today three signals lie by omission: a CI verdict that announces a stale
page as green, a host-liveness detector whose evidence window empties
itself, and a worker host no detector can see. Restore the verdict first
(it gates every other diagnosis), then the detector, then attributability.
**Last reviewed:** 2026-09-30
**Worktree:** `monitors-that-go-quiet`

## Do next

1. **gr456236** — scripts/main-ci-status runs its staleness guard only on
   the RED path, so a stale `gh run list` page reads as "main green"; policy
   runs this script before any local gate, so a false green is licence to
   skip the gate. Observed twice in one session. Already STATUS:in_review
   with a fix branch (gripe_456236, from jo456570, auto-fix lane td456543):
   review-and-land, which is why it outranks larger work.
2. **backlog/host-dark-ages-out-with-worker-logs-retention.md** — the
   host-dark lookback equals worker_logs retention, so a host dark longer
   than retention becomes permanently invisible: the alert ages out because
   the evidence was deleted. Prevents a broken host reading as
   decommissioned. Latent, no incident, hence below a defect reproduced
   twice.
3. **backlog/unnamed-container-host-wrote-211k-worker-logs.md** — an
   unnamed container host wrote 211K rows 2026-08-30 → 09-09 then went
   silent, invisible to host-dark by two deliberate exclusions (gr306275,
   gr331348). Last because it is an investigation (attributability), not a
   defect; nothing can be specced until answered.

## Horizon

1. **backlog/main-stays-gated.md** — successor to Do-next 1: gr456236
   restores trust in reading main's verdict, this restores there being
   one (qland gate-bypass pre-check; a cancelled main run leaves src changes
   with no verdict, the failure hit 2026-09-29 when three consecutive main
   runs were cancelled by the next sibling push). The post-merge watcher
   belongs here.
2. **backlog/alert-failure-id-registry.md** — status ready; stable failure
   ids make "did host-dark fire, for which host" addressable instead of SQL
   archaeology. Leverage over Do-next 2–3 and shippable now.
3. **backlog/self-healing-spine.md** — Layer 1 owns worker identity, Layer 2
   the condition registry; Do-next 2 and 3 exist because a host has no
   durable identity separating "ephemeral by design" from "vanished", so
   attributability-by-container is a slice here. Last: largest, no
   independently shippable piece touching this thread.

## Parked

- (none)

## No action needed

- **gr346534** — the post-merge-run gripe gr456236 cites; soft-deleted, do
  not chase.

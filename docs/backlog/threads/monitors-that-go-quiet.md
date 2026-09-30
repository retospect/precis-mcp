# monitors that go quiet

**Status:** ends when main always has a verdict and every fleet alert is
addressable by failure id and host identity, so silence means healthy. Two
halves landed 2026-09-30: main-ci-status no longer announces either verdict
off a cached listing, and a main push is now gated against the delta since
the last sha with a real shard verdict, so a cancelled run in a qland burst
no longer leaves src the docs lane waves through. Today qland itself is still
the unguarded path, and two fleet signals lie by omission: a host-liveness
detector whose evidence window empties itself, and a worker host no detector
can see.
**Last reviewed:** 2026-09-30
**Worktree:** `monitors-that-go-quiet`

## Do next

1. **backlog/main-stays-gated.md** — down to one section: `scripts/ship
   --quick` should run ruff + mypy (~3 min, no pytest, no gate slot) before
   the squash-merge. Every one of the four failures that held main red for
   ~6h on 2026-09-25 was in that class, and two sessions diagnosed them
   independently. First because it is the only item here with observed,
   repeating cost, and because it is what makes the lane fix that landed
   today unnecessary rather than load-bearing.
2. **backlog/host-dark-ages-out-with-worker-logs-retention.md** — the
   host-dark lookback equals worker_logs retention, so a host dark longer
   than retention becomes permanently invisible: the alert ages out because
   the evidence was deleted. Prevents a broken host reading as
   decommissioned. Latent, no incident, hence below an observed failure.
3. **backlog/unnamed-container-host-wrote-211k-worker-logs.md** — an
   unnamed container host wrote 211K rows 2026-08-30 → 09-09 then went
   silent, invisible to host-dark by two deliberate exclusions (gr306275,
   gr331348). Last because it is an investigation (attributability), not a
   defect; nothing can be specced until answered.

## Horizon

1. **backlog/alert-failure-id-registry.md** — status ready; stable failure
   ids make "did host-dark fire, for which host" addressable instead of SQL
   archaeology. Leverage over Do-next 2–3 and shippable now.
2. **backlog/self-healing-spine.md** — Layer 1 owns worker identity, Layer 2
   the condition registry; Do-next 2 and 3 exist because a host has no
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

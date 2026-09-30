---
status: ready
title: surface-review follow-ups — resume pointer for pass #1's findings
pillar: platform
prio: high
---

# surface-review pass #1 — what's left, in order

Resume pointer for the work that came out of the first `/surface-review`
(2026-09-29). Everything below is filed; this file only carries the **order**
and the **cross-item constraints**, which live in no single item.

Pass #1's numbers and method: `docs/runbooks/surface-review.md` `## Log`.
The miner itself: `scripts/mine-sessions/README.md`.

## Done, landed

Landed: `scripts/mine-sessions/` (committed extractor + 12 detectors +
evidence cards + scoreboard), the `/surface-review` command + runbook +
14-day cadence script, three doc-staleness fixes, and the `gen-schema`
statement_timeout fix with `docs/reference/schema.md` regenerated 58→130
tables. Also landed: `policy-gates-must-fail-distinguishably`.

**Deployed and gated.** Both landings were inside the deploy session's
2026-09-30 full gate of the integrated main and are on the fleet, so the
schema regen and the two changed product skills (`precis-fisheye-help`,
`precis-status-help`) are live and validated.

## The order, and why it is an order

1. **`gripe-comment-timeline-uncapped`** — extract the shared capped-section
   renderer; make the existing links cap its first caller.
2. **`singleton-id-no-batch-form`** — make `_coerce_id` the id normalizer
   (scalar-or-list in, list out, clean `BadInput` otherwise).

**1 gates 2.** A batch `get` of 50 gripes whose comment timelines are
uncapped is worse than the singleton loop it replaces. Doing 2 first
actively makes things worse, which is the kind of thing that is obvious in
sequence and invisible in a backlog list.

Both are deliberately framed as shared-seam fixes rather than point fixes —
Reto's call, 2026-09-29: *"copying a decided convention smells like not
DRY."* The links cap already existed and the lesson did not transfer to
comments; a second cap constant beside the first would repeat that.

3. **`draft-write-latency-whole-draft-rescan`** — needs a design pass, not a
   patch: chunk-grounded `src_pos` has to survive whatever replaces the
   whole-draft rescan.
4. **Ledger instrumentation** — `mcp-surface-economy.md`, the
   "Instrumentation blocker" section: `result_bytes`, the reserved-but-NULL
   `result_count`, and a correlation key that is actually populated
   (`agentlog_id` is set on 0.03% of rows). Needs a migration ⇒ `/go` only.
   This is what turns the next pass's biggest number from an extrapolation
   into a measurement.
5. **`gr456287`** (harness worktree guard, 308 refusals / 97 of 99 sessions)
   — in-repo lever is only the accepted-shape cheat sheet into
   `docs/conventions/container-ops.md`; the guard itself is a harness
   built-in. See `worktree-path-guard-false-positives.md`.

## Next pass

Due **2026-10-13** (`scripts/surface-review` prints DUE). It should be far
cheaper than pass #1 — the extraction is code now and `scoreboard.json`
carries a `schema_version`, so pass N is comparable with pass N−1.

Two things the runbook already encodes but that are worth knowing before
reading it: size the random arm at **30–40** (pass #1 ran 12 and that arm
still produced the most valuable finding), and read the byte/retry columns
before the error table (the error rate is 0.8%, so it is not where the waste
is).

## Traps that cost this session time

- **Miner artefacts live outside the repo** (`~/.cache/precis-mine-sessions/
  <worktree>/`), because the secret gate scans the working tree rather than
  the index. Don't move them back in; `scripts/mine-sessions/outdir.py`
  explains it.
- **Only 2 fleet-wide gate slots exist.** `scripts/test` queues behind
  siblings; a quiescent run is waiting, not hung. Read the holder file, not
  `docker ps`.
- **`OSError: [Errno 23]` from a policy gate is not a finding** — see item
  `policy-gates-must-fail-distinguishably` and
  `memory/gate-errno23-fd-exhaustion.md`.

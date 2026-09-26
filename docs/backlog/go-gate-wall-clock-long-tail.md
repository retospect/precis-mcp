---
status: idea
title: /go's local gate takes 2h10m and cannot win a CAS race — split the long tail
---

# /go's local gate takes 2h10m and cannot win a CAS race

## What
Measured 2026-09-25 on melchior (warm gate container, `-n 6`):
`1 failed, 22695 passed, 74 skipped, 4 xfailed in 7712.60s (2:08:32)`.

The distribution is the problem, not the total. The run reaches 88% in the
first ~25 min and then spends **over 90 minutes on the last ~5%** — a
compute-heavy tail (the pcb/surface/atomistic kernels) grinding at 100–200%
CPU with five of six xdist workers idle. `py-spy` can't name the offenders:
the gate container has no `SYS_PTRACE`, so `py-spy dump` fails with
`Permission denied`.

## Why it matters
`/go` runs the full suite deliberately — a deploy ships to the whole cluster,
so the gate must be authoritative. But at 2h10m the gate **cannot win a CAS
race against a qland burst**. On 2026-09-25 two consecutive full gates were
invalidated before reaching their squash-merge because `main` moved
underneath them (~30 min between sibling lands). The merge only landed after
switching to `scripts/ship --remote`, which shards across 6 Linux runners and
returns in ~12 min.

So today `/go` has a gap: its own gate is the slowest path to `main` *and*
the one most likely to be wasted, while the faster path skips the
diff-coverage gate and the mutation pass.

## Options
- **Profile first.** `--durations=25` on a full run names the tail. Nothing
  below should be designed before that list exists — the 90 minutes may be
  five tests or five hundred.
- Give the gate container `SYS_PTRACE` so `py-spy dump` works on a live hang.
  Cheap, and today it was the difference between "is it wedged or slow?" and
  a 15-minute guess. (Distinct from the two 95–98% hang signatures in
  auto-memory `gate-lane-docs-only-trap`; this one was genuinely just slow.)
- Mark the tail `@pytest.mark.slow`; `/go`'s gate runs fast + the impacted
  slow tests, with the full slow set nightly and on the remote gate.
- Or: make `/go` use the remote gate for the merge and run diff-coverage +
  mutation locally in parallel, so the CAS race is fought at 12 min instead
  of 2h10m.

## Not in scope
The remote gate's own shape (lint + 6 Linux shards) — that already works.

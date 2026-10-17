---
status: in-progress
title: surface-review follow-ups — resume pointer for pass #1's findings
pillar: platform
prio: high
---

Status 2026-10-17: every pass-1 item shipped. Pass #2 is OVERDUE (was due 2026-10-13); run it and record its findings here.

# surface-review pass #1 — residue

Shipped (pass #1 miner, runbook, schema regen, shared render cap, batch
`get`/`tag`/glossary `put`, draft-write latency, ledger instrumentation):
b9f44519f, c37b29150; per-item history in `git log`. Pass #1's numbers and
method: `docs/runbooks/surface-review.md` `## Log`. The miner:
`scripts/mine-sessions/README.md`.

Open: `gr456287` (harness worktree guard, 308 refusals / 97 of 99 sessions) —
the in-repo lever (accepted-shape cheat sheet) lives in
`docs/conventions/container-ops.md`; the rest is upstream-only in
`worktree-path-guard-false-positives.md`.

## Next pass

Due **2026-10-13**, overdue (`scripts/surface-review` prints DUE). It should be far
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
- **`OSError: [Errno 23]` from a policy gate is not a finding** — policy
  scans fail distinguishably on OSError since 2026-10-09; see
  `memory/gate-errno23-fd-exhaustion.md`.

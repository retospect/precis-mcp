---
status: done
---

# Main stays gated

Grouped 2026-09-26 from 2 items; all three shipped 2026-09-30. Kept only until
the numbers in the third have survived a week of real bursts — delete then.

- **A cancelled main run leaves its src changes with no verdict** — closed.
  check.yml's main-push lane now scopes to the delta since the last sha whose
  `test-linux` check-runs were all successful (`scripts/last-gated-main-sha`),
  so a docs-only push sitting on ungated src inherits the full shard lane
  instead of the docs one. The range can only widen, so the failure direction
  is over-gating; a lookup that cannot answer gates fully.
- **qland is the gate bypass** — closed. `scripts/ship --quick` now runs a
  pre-qland lint (ruff autofix, mypy, import contracts) before the
  squash-merge: no pytest, no test DB, no fleet gate slot, ~3 min in the warm
  container. Every one of the four failures that held main red ~6h on
  2026-09-25 was in that class. `PRECIS_QLAND_LINT=0` is the escape hatch, for
  a broken toolchain rather than a red check. The qland skill also now says to
  run the tests you added — the one failure of that day a lint cannot catch
  was a characterization test whose author demonstrably never ran it.

- **qland onto a main that has no current verdict** — closed. The pre-qland
  lint blocks on *your* change; it cannot see that main itself has outrun its
  verdicts. `scripts/ship --quick` now asks
  `scripts/last-gated-main-sha --age-hours` how long ago main's last all-green
  shard matrix finished, and **warns at 24h, refuses at 48h**
  (`PRECIS_QLAND_DRIFT_WARN_HOURS` / `PRECIS_QLAND_DRIFT_REFUSE_HOURS`;
  `PRECIS_QLAND_DRIFT_OVERRIDE=1` to land anyway).

## The numbers, and why these ones

**Reto 2026-09-30: "a day or two."** Written here as 24h/48h so the next
reader can veto a figure rather than a phrase — if either is wrong it is one
constant, not a redesign.

Measured in **time, not commits behind**. Twenty docs commits in five minutes
is not the risk three src commits over two days is, and the thing that makes
an ungated main expensive is how far the bisect has to reach back, which
tracks wall-clock far better than commit count.

Both failure directions were live when this was held, and the split answers
each:

- **Refusing** strands trees behind a red nobody has claimed — the failure
  mode `scripts/main-ci-status`'s ownership signal exists to prevent. At 48h
  that is a state somebody has to fix regardless; the override exists for the
  case where they have.
- **Warning only** is roughly what the existing `📦 N commit(s) not yet
  deployed` line already does, and a warning nobody acts on is how main
  reached 20-odd ungated commits on 2026-09-30. Hence a refusal at all.

**An unknown age never refuses.** `--age-hours` prints nothing when it cannot
tell — no `gh`, no auth, nothing gated inside the walk window — and the guard
treats empty as "do not gate on this". Refusing on a lookup that could not be
answered would let a GitHub outage stop every tree in the fleet from landing,
which is a worse failure than the ungated main this guards against. The
gr456236 class of stale-API answer therefore degrades to silence, not to a
block.

The age is the **newest** `completed_at` across the matrix, not the commit
date: a verdict lands after the commit it judges, so commit date overstates
drift, and this number gates a hard refusal.

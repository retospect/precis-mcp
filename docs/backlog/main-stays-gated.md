---
status: draft
---

# Main stays gated

Grouped 2026-09-26 from 2 items; both shipped 2026-09-30, leaving one
follow-on below that needs a number decided before it can be written.

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

## Follow-on: refuse to qland onto a main that has no current verdict

The pre-qland lint blocks on *your* change. It does not stop you stacking onto
someone else's red, or onto a main whose last gate-shape run is not green.
`scripts/last-gated-main-sha` now answers exactly that question, so the check
is cheap to add: if the last sha with an all-green shard matrix is many
commits behind head, the burst has outrun its verdicts and what is needed next
is a `/go`, not another qland.

Held rather than done, because the response is a judgement call and guessing
it wrong is expensive in both directions:

- **Refusing** strands trees behind a red nobody has claimed — the failure
  mode `scripts/main-ci-status`'s ownership signal exists to prevent — and
  would let a stale verdict (the gr456236 class) block shipping outright
  rather than merely misinform.
- **Warning only** is roughly what the existing `📦 N commit(s) not yet
  deployed` line already does, and a warning nobody acts on is how main
  reached 20-odd ungated commits on 2026-09-30.

So it wants a number: how far behind its last verdict main may drift before a
qland is refused rather than warned. Decide that, then it is a few lines in
`scripts/ship`.

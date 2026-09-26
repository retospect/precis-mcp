---
status: draft
---

# Main stays gated

Grouped 2026-09-26 from 2 items that are sub-parts of one deliverable (each keeps its own section below; the originals are in the history). Split a section back out only when it becomes independently shippable.

## qland is the gate bypass — add a cheap pre-qland check

_Grouped 2026-09-26; was `investigate-mypy-red-commit-bypassed-ship-gate`, status idea._

### Answered: how a mypy-red commit reaches main
The original question (2026-08-12, `13b28625` mypy-red on `origin/main`) is
answered, and not by a force-push or a host/container mypy divergence:
**`/qland` is an intentional ungated merge path** (`scripts/ship --quick`:
commit WIP → sync → squash-merge, no gate). Nothing is bypassed; the gate
was never run.

2026-09-25 is the same failure at scale. A qland burst put `main` in a state
that was red four independent ways at once:

- ruff format drift in 3 pcb test files (`3c8db49a`)
- `tests/test_backlog_groom.py:371,395` — `.fetchone()[0]` with no `None`
  check, a mypy `[index]` error
- 4 mypy errors in `tests/test_quest_tagging.py` (`7707cb1c`) — an invariant
  `list[tuple[int, float]]` vs `list[tuple[int, float | None]]`, and
  `SimpleNamespace` passed where `argparse.Namespace` was expected
- `test_net_islands_false_flags_a_genuinely_touching_diagonal_escape_as_split`
  — `aa861102` (07:23) fixed the disk model, then `3c8db49a` (08:05, branched
  *after* it) added a characterization test asserting the bug was still live

Cost: main sat red for ~6h, two sessions independently diagnosed the same
four failures, and one `/go` burned two full 2h10m local gates before the
first one even reached its squash-merge.

### The cheap fix
Every one of those four is caught by **ruff + mypy alone — no pytest**, which
runs in ~3 min against the warm container. `scripts/ship --quick` should run
that much before the squash-merge. It keeps qland's reason for existing
(don't pay the 2h pytest suite per tree in a burst) while making it
structurally impossible to qland a commit that reddens the *next* tree's
gate — which is what actually costs the hours.

The residual risk qland still accepts, deliberately: a test-level failure
that only pytest sees. That is the trade qland is for.

### Also worth doing
- `3c8db49a`'s test was written against a tree that already contained
  `aa861102`'s fix, so the author cannot have run it. A pre-qland check
  catches the lint/mypy class but not this one — only running the new test
  once does. Worth a line in the qland skill: *run the tests you added.*

### Not in scope
The four fixes themselves — all shipped in `4db6836b`.

## A cancelled main run leaves its src changes with no verdict

_Grouped 2026-09-26; was `main-lane-picker-leaks-ungated-src`, status idea._

### What
On 2026-09-26 `origin/main` reached `98889951` with a **green** check.yml run
that had `test-linux` and `test-other` *skipped* — only `plan`, `lint`,
`test-docs` ran. The last main sha with a real 6-shard Linux verdict was
`9bc27528`, three commits earlier, and `git diff --stat 9bc27528 98889951`
shows 35 changed lines in `src/precis/store/_pcb_ops.py` plus a new
218-line test file. So a green main was carrying src nobody had gated.

### Why it happens
Two mechanisms compose:

1. **The lane picker scopes to the push's own delta.** On main,
   `.github/workflows/check.yml` sets `range="${BEFORE}..HEAD"` — the
   squash-merge's delta, not the delta since the last *gated* sha. That is
   correct in isolation: each push gets gated on its own change.
2. **The concurrency group cancels superseded main pushes.**
   `cancel-in-progress` is true for non-schedule events, keyed on
   workflow+ref+shape. A qland burst pushes main several times in minutes,
   so the earlier runs are cancelled.

Together: `a10708e9` and `eb6a846c` (the pushes that carried the `_pcb_ops.py`
change) were both **cancelled**, and the next push `98889951` had a docs-only
delta, so it took the docs lane and went green. Neither the code nor any
later run ever tested it. `conclusion: cancelled` is not a failure, so nothing
surfaces.

This is a superset of the `gate-lane-docs-only-trap` note in auto-memory: the
trap there is a docs-lane green being mistaken for a code gate. Here the
docs lane is the *correct* verdict for that push and main is still ungated.

### Cost seen
A `/go` deploy could not use main's green run as its gate. Recovery was a
bare `gh workflow run check.yml --ref main` — a `workflow_dispatch` sets no
`range`, so `docs_only` stays false and the full gate shape runs on the
current head. That works, but it is a manual step nobody is told to take.

### Options
- **Make the main-push range start from the last green *gate-shape* run**, not
  from `github.event.before`. Then a docs-only push that sits on ungated src
  inherits the full lane. Costs a lookup of the last successful gate run.
- Or exempt main from `cancel-in-progress` (each squash gets its verdict,
  at the cost of runner slots during a burst).
- Or have `scripts/ship --quick` refuse to qland onto a main whose last
  gate-shape run is not green/current — pairs with the pre-qland lint+mypy
  check proposed in `investigate-mypy-red-commit-bypassed-ship-gate.md`.

### Not in scope
The `ci/**` pre-merge gate — there the three-dot `origin/main...HEAD` range is
the branch's whole change, so this shape cannot occur.

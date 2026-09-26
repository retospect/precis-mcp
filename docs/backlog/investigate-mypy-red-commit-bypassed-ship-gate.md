---
status: idea
title: qland is the gate bypass — add a cheap pre-qland lint+mypy check
---

# qland is the gate bypass — add a cheap pre-qland check

## Answered: how a mypy-red commit reaches main
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

## The cheap fix
Every one of those four is caught by **ruff + mypy alone — no pytest**, which
runs in ~3 min against the warm container. `scripts/ship --quick` should run
that much before the squash-merge. It keeps qland's reason for existing
(don't pay the 2h pytest suite per tree in a burst) while making it
structurally impossible to qland a commit that reddens the *next* tree's
gate — which is what actually costs the hours.

The residual risk qland still accepts, deliberately: a test-level failure
that only pytest sees. That is the trade qland is for.

## Also worth doing
- `3c8db49a`'s test was written against a tree that already contained
  `aa861102`'s fix, so the author cannot have run it. A pre-qland check
  catches the lint/mypy class but not this one — only running the new test
  once does. Worth a line in the qland skill: *run the tests you added.*

## Not in scope
The four fixes themselves — all shipped in `4db6836b`.

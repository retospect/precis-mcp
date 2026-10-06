---
status: ready
title: Every main sha gets a CI verdict; the round deploys the newest green one — no local gate, no cancelled runs
pillar: platform
prio: high
---

# Every main sha gets a CI verdict; the round deploys the newest green one

Reto, 2026-10-03: "push should not break build" — set off as p1 after an
independent critique of the first (release-branch) plan. Review item:
`~/.claude/projects/-Users-reto-precis-mcp/review-queue/open/organizer-release-branch-1.md`
(v2 holds the critique's findings and the two decisions still open).

## Motivation / why

`check.yml` keys its concurrency group on the ref with `cancel-in-progress`
for every non-schedule event, so each qland to `main` (~7.7/h on
2026-10-02/03, 12-minute runs then) cancels the previous run: `main` had no
completed verdict for most of 2026-10-03 and a red `type: ignore` ratchet
hid for 12 h behind cancelled runs. Two things depend on those verdicts
and are silently broken today: `scripts/last-gated-main-sha` walks only 25
first-parent commits (`DEFAULT_LIMIT = 25`, ~3 h of main), so `scripts/ship
--quick`'s drift guard has printed "age unknown — never refuse" since
2026-10-02 ~13:00Z; and the round's deploy gate is a LOCAL full run that
holds the ship lock (~10 min, every qland stalls) and gates whatever `main`
is at that second. A release branch with cherry-picks was considered and
rejected: `gated`/`prod` are fast-forward-only (`scripts/lib/env-pointers.sh`)
and a cherry-picked sha is never an ancestor of a later main.

## In scope

1. **Hygiene tests into the pre-qland lint** (`scripts/ship --quick`): the
   `type: ignore` ratchet (`tests/test_type_ignore_ratchet.py`), the
   posix-only guard (`tests/test_posix_only_guards.py`) and the other
   DB-free hygiene tests already in the lint set's spirit. Ship first and
   alone; it lowers the red rate regardless of the rest.
2. **`check.yml`: stop cancelling `main`.** `cancel-in-progress:
   ${{ github.event_name != 'schedule' && github.ref != 'refs/heads/main' }}`.
   `ci/**` and pull-request refs keep cancelling (a ship retry force-pushes
   the same ref). Document the slot cost in the workflow's job-budget note
   (≈1.5 concurrent main runs ≈ 12 of the 20 jobs on average; bursts queue
   per job).
3. **`scripts/last-gated-main-sha`**: time-bounded walk (48 h) or ≥ 400
   first-parent commits, `--age-hours` meaning stated in `--help`; the
   verdict is the sha-keyed check-runs API (lint + all 6 `test-linux`
   success), never `gh run list`.
4. **`scripts/round gate`** prints the candidate (newest green main sha),
   its verdict age, and how many docs-only commits main is ahead of it;
   **`scripts/round deploy`** fetches, ff's `gated` to the candidate and
   runs `scripts/deploy <40-char sha> --pinned`. Neither takes the ship
   lock. A red verdict names the failing test for routing.
5. Docs in the same commits: CLAUDE.md "Ship workflow" (the round no longer
   runs a local full gate), `deploy/README.md` refs table, `.claude/commands/round.md`,
   the `/fleet` skill's Round step.

## Explicitly NOT in scope

- A `release`/`deploy` branch, cherry-picks, hotfixes outside `main`.
- Changing `/go` (`scripts/ship --mutate --full` + pinned deploy from a
  feature tree) or `/qgo`.
- The nightly full matrix (stays on `main`, schedule, never cancelled).
- GitHub merge queue (revisit when qland volume drops).

## Acceptance criteria

- A qland that breaches the `type: ignore` ratchet is refused by
  `scripts/ship --quick` before the push (test: bump a count locally,
  observe the refusal).
- Two pushes to `main` 2 min apart both reach a completed `check.yml`
  conclusion (neither `cancelled`); a force-push to the same `ci/<branch>`
  ref still cancels the superseded run.
- `scripts/last-gated-main-sha --start origin/main --age-hours` prints a
  number when the newest green sha is up to 48 h / 400 commits back, and
  `scripts/ship --quick` warns/refuses at 24 h / 48 h again (test with
  `PRECIS_QLAND_DRIFT_*` overrides).
- `scripts/round gate` on a main whose head is docs-only prints the
  previous code sha as candidate with the "N docs-only commits behind"
  line; `scripts/round deploy` moves `gated` and `prod` by fast-forward
  only and refuses when the candidate is not an ancestor of `main`.
- No `scripts/test` run and no ship-lock acquisition occurs during a
  round's gate + deploy (check the lock holder file during a dry run).

## Target + blast radius

`.github/workflows/check.yml` (concurrency; runner budget for every
`ship --remote` gate), `scripts/ship` (pre-qland lint set, drift guard),
`scripts/last-gated-main-sha`, `scripts/round` (new verbs), `scripts/deploy`
(called, not changed), CLAUDE.md / deploy/README.md / round.md / fleet skill.
Risk: more queued jobs on GitHub → slower `ship --remote` gates; measure for
a week and report in the thread file.

## Open questions / decisions log

- Reto: accept the un-cancel slot cost as is, or gate-shape only when the
  diff touches src/tests/deploy (already `plan`'s behaviour — likely moot).
- Reto: maximum candidate age the round may deploy without waiting for a
  newer green (proposal 6 h).
- Decided 2026-10-03 (critique): no release branch; candidate = exact main
  sha; the drift guard's blind window is a bug to fix, not a premise.

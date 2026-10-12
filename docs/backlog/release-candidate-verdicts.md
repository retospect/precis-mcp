---
status: draft
title: Round candidate age cap — Reto's ruling on DEFAULT_MAX_CANDIDATE_HOURS
pillar: platform
prio: high
---

# Round candidate age cap — Reto's ruling on DEFAULT_MAX_CANDIDATE_HOURS

Residue of the 2026-10-03 release-candidate-verdicts work (474991803): every
main push runs `check.yml` to completion, `scripts/last-gated-main-sha` walks
48 h over GraphQL, and `scripts/round gate|deploy` ships the newest green main
sha with no local gate and no ship lock. The one-week `ci/**` queue-delay
report is in `threads/ship-gate-ci.md`.

## Open — needs Reto

- **Maximum candidate age** the round may deploy without waiting for a newer
  green verdict. Proposal: 6 h, which is what `DEFAULT_MAX_CANDIDATE_HOURS`
  in `scripts/round` holds today. A different ruling changes only that
  constant (and its test in `tests/test_round_gate.py`).

## Settled

- The un-cancel slot-cost question is moot: main pushes share one
  never-cancel-running group (one running + one pending run, Reto,
  review-queue ship-gate-ci-1, 2026-10-03), so main never holds more than
  one run's jobs.

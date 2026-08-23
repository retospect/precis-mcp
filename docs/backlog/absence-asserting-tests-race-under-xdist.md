---
status: draft
title: tests asserting global absence race against sibling xdist workers
prio: normal
---

# tests asserting global absence race against sibling xdist workers

## Motivation / why

Observed 2026-08-23 on a full `scripts/ship` gate: two failures, both of which
pass in isolation in ~10s.

```
FAILED tests/test_llm_catalog.py::TestReconcile::test_no_drift_when_proxy_unknown
FAILED tests/test_quest_reweight.py::TestRotationReweight::test_no_op_without_active_quests
```

Neither is a load or OOM symptom — they are racy by construction. Both assert
the *absence* of a globally-scoped condition in a store that xdist workers
share:

- `test_no_drift_when_proxy_unknown` ends on `assert not
  _open_drift_alerts(store)` — an unfiltered "are there any open drift alerts"
  read. Any concurrent worker minting one fails it.
- `test_no_op_without_active_quests` asserts `td<leaf_a>` precedes
  `td<leaf_b>` in `TodoHandler.search(view="doable")` — a corpus-wide ranked
  list. A concurrent worker creating an active quest reweights that ordering
  and flips the pair.

This is distinct from the three known infra flakes (OOM-137, colima bind-mount
`import file mismatch`, test-DB `does not exist`): those come from resource
pressure, this one is a missing scope predicate. Sibling gate load only widens
the interleaving window, so it presents *as* a load flake and gets re-run
rather than fixed.

Cost of not fixing: a red gate that is indistinguishable from a real red until
someone re-runs it, on a ship path that already takes ~10 minutes per attempt.

## In scope

- Scope both assertions to rows the test itself created — filter
  `_open_drift_alerts` by the model/ref the test upserted; assert the relative
  order of `leaf_a`/`leaf_b` among the test's own todos rather than their
  index in the global doable list.
- Sweep for the same shape elsewhere: assertions of the form `assert not
  <unfiltered list>(store)` or an index comparison against a corpus-wide
  ranked result.

## Explicitly NOT in scope

- Per-test database isolation, or dropping xdist. The fix is a scope
  predicate in the assertion, not a change to the harness.
- The reweighting and drift-detection logic itself — both behaved correctly.

## Acceptance criteria

- Both tests pass under deliberate adversarial concurrency (a parallel worker
  minting a drift alert / an active quest) rather than only in a quiet window.
- No remaining test asserts emptiness of an unscoped corpus-wide query.

## Target + blast radius

`tests/test_llm_catalog.py::TestReconcile`,
`tests/test_quest_reweight.py::TestRotationReweight`. Test-only — no source
change, so no deploy risk.

## Open questions / decisions log

- Is there a reusable fixture-level scope handle (a per-test tag or actor
  slug) already available to filter on, or does each call site need its own
  predicate? Unchecked.

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

Third instance, 2026-08-27 (same signature, different global): a full gate
failed `tests/test_runtime.py::test_build_runtime_no_database` — 1 failed /
15171 passed — which **passed in isolation in 4.6 s**. It pops
`PRECIS_DATABASE_URL` and asserts `"memory" not in rt.hub`, i.e. that
`build_runtime()` finds no store. But the env var is not the only DSN source
(`precis.secrets` also resolves an adopted process store / pgpass), so a
sibling worker that has adopted a store makes the runtime stateful and the
absence assertion flips. Same missing-scope shape: the test asserts a
*process-global* absence it does not control. Note this one is not even
store-scoped — it races on interpreter/process state, so a store-scoping fix
alone would not cover it.

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

## Second sighting — 2026-09-24, `tests/test_se_block_uid.py`

Same signature, thirteen months on and in a different module. A full
`scripts/ship --mutate --full` gate on integrated main went RED with five
failures, all in one file:

```
FAILED tests/test_se_block_uid.py::test_put_records_the_governing_scenario
FAILED tests/test_se_block_uid.py::test_presets_differ_in_weights_and_lifetime
FAILED tests/test_se_block_uid.py::test_drc_view_records_the_scenario_too
FAILED tests/test_se_block_uid.py::test_unknown_scenario_is_rejected_before_anything_is_written
FAILED tests/test_se_block_uid.py::test_scenario_survives_a_later_put
5 failed, 22597 passed, 74 skipped, 4 xfailed in 17179.15s (4:46:19)
```

Re-run of that file alone against the same tree, serially
(`scripts/test tests/test_se_block_uid.py -n0`, the named-file gate-queue
bypass): **37 passed in 6.72s**. So the failures are an artefact of the
parallel gate, not of the code — the file's owning commits (the design-core
uid cutover) were already main ancestors at the time.

Two aggravating conditions worth recording, because they make this class
more likely to bite rather than less:

- **Three `--mutate --full` ships were contending on the same host**, so the
  gate ran under heavy thread oversubscription (gr345784). The run took
  4h46m against a nominal ~10 min local gate.
- At that duration the verdict is also vulnerable to the separate
  "gate result expires mid-run" failure mode, so a RED gate of this length
  carries two independent reasons to be re-verified before anyone treats it
  as a real failure.

The cost here was not the lost gate — it was the five hours spent producing a
verdict that had to be thrown away, plus a second ship blocked on the lock for
9h12m behind it without running a single test.

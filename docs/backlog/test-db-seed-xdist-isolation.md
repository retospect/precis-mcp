---
status: draft
prio: medium
---

# Test db seed xdist isolation

Grouped 2026-09-26 from 3 items that are sub-parts of one deliverable (each keeps its own section below; the originals are in the history). Split a section back out only when it becomes independently shippable.

## `component_specs`' category-scoped seed vanishes on the gate's clones

_Grouped 2026-09-26; was `component-seed-guard-misses-scoped-specs`, status draft, prio medium._

Found 2026-09-05 while shipping se off-the-shelf rung 2b. Cost several
gate cycles to localize, so the evidence is written down in full rather
than summarized.

### Symptom

A `component` series mint reports a **partial** write:

```
specs: 4 written, 0 already current, 4 skipped
       (drive_type; length; thread_pitch; thread_size)
```

The four skipped are exactly migration **0093**'s *category-scoped* core
specs. The four written are **0152**'s *universal* geometry specs. The se
catalog derivation then reports `screw needs length`, three layers away
from the actual fault — which is what made it expensive.

### Where it does and doesn't happen

- **`scripts/test` (dev container, persistent `precis_test`): passes.**
  A direct query of that DB shows all 24 core specs including `length`.
- **`scripts/ship`'s gate: fails**, reproducibly (3 runs). The gate builds
  a co-located tmpfs DB and clones it per xdist worker; the clones are
  dropped at the end of the run, so a post-hoc query of the surviving
  template shows a *healthy* DB and proves nothing. **That mismatch sent
  the first investigation down a false trail** — verify against the DB the
  failing process used, not the one left behind.

### What is ruled out

- **Not truncation.** `component_categories` and `component_specs` are
  both in conftest's `_PRESERVE_TABLES`. `TRUNCATE ... CASCADE` cascades
  to tables *referencing* the truncated one; `component_specs` references
  only `component_categories`, which is preserved. Nothing can delete
  category-scoped spec rows while leaving universal ones — so **those rows
  were never inserted** in that DB.
- **Not a mutation.** `component_spec_mint` is a plain INSERT (a
  collision raises), and nothing else in `src/` or `tests/` writes
  `component_specs`.
- **Not the registry being empty.** 0152's universal specs are present,
  so the table exists and was written by the tail migration run.

### Why the existing guard doesn't catch it

The baseline dump (`migrations/baseline/schema.sql`) carries the
`_migrations` ledger but **no seed rows** — `COPY public._migrations` is
the only data statement in the file. So a DB built from the baseline has
0093 marked applied, its `INSERT ... ON CONFLICT DO NOTHING` seeds never
re-run, while a migration *newer* than the baseline (0152) runs for real.

`conftest._ensure_component_seed` exists for exactly this and re-applies
0093's SQL — but it originally gated on `component_categories`' core
count alone, so an intact category tier made it return early and leave
the specs unseeded. **Fixed in this ship** to also require a core spec
with `category_id IS NOT NULL` (a bare "any core spec" test is
insufficient: 0152 seeds ten universal core specs, so the table is
non-empty even when every 0093 row is missing).

**That fix was necessary but not sufficient** — the gate still failed
after it, and no `re-applying 0093's seed` warning appeared in the run
log (though conftest session-setup logging may simply not be captured).
So the remaining question is *when* the guard runs relative to the
per-worker clone: `_claim_template_maintenance` lets only the first
worker seed the template, under an advisory lock held across the clone,
which should be sufficient — but the observed clone state says otherwise.

### Current mitigation

`tests/test_se_catalog_binding.py::_ensure_fastener_specs` re-applies
0093 through the store's own connection when the category-scoped tier is
missing, so those tests guarantee their own precondition. `_bolt` still
asserts the mint wrote everything, so a recurrence fails loudly at the
mint instead of surfacing as a catalog error.

### Next step for whoever picks this up

Instrument the clone path, not the template: log
`count(*) FROM component_specs WHERE category_id IS NOT NULL` from inside
a test on a gate clone, and compare against the template at the same
moment. That single number distinguishes "the template was never seeded"
from "the clone diverged from a seeded template", which is the fork the
investigation above could not close from outside the run.

Any test that depends on 0093's category-scoped seed is exposed, not just
se's — this is test-infrastructure, not an se defect.

**It generalizes to every later category-scoped migration**, confirmed
2026-09-15: migration 0163 (`head_form`/`point_type`/`head_angle`/
`drive_code`, all scoped to `fastener`) has exactly the same exposure, and
`tests/test_se_fasten_seatclamp.py` replays it the same way 0093 is
replayed. Each new scoped seed otherwise needs its own workaround, which
is the cost of leaving this open — and the reason the fix belongs in the
clone path rather than in one more test fixture.

## test_se_order fails when `unit_cost` was auto-minted under another category in the same xdist worker

_Grouped 2026-09-26; was `test-se-order-unit-cost-order-dependency`._

Bug (found 2026-09-19 by a hybrid local gate on integrated main; does not
reproduce on GitHub, where sharding separates the files). Three
`tests/test_se_order.py` tests red with `BadInput: spec='unit_cost' applies
to category 'bearing', not 'fastener'|'widget'` — i.e. in that worker's DB
clone the 0093 universal core spec `unit_cost` was absent and
`tests/test_se_bom.py` (`bearing-608` at ~L824) auto-minted it as a
bearing-scoped *proposed* spec, which `component_specs` (preserved across
tests, "core + proposed") then carried into every later test. The
template itself had `unit_cost` universal afterwards, so the loss is
per-clone and mid-run; mechanism not yet identified (`_ensure_component_seed`
runs only at template maintenance, and its guard probes category-scoped
core specs, so a missing *universal* 0093 row is invisible to it).

Owner anchor: `tests/conftest.py::_ensure_component_seed`,
`src/precis/handlers/component.py::_mint_spec` ("never universal").

Test: run `tests/test_se_bom.py tests/test_se_order.py` in one worker
(`-n 0`, se_bom first) against a clone whose `unit_cost` row was deleted —
must stay green; then find who deletes it.

## tests asserting global absence race against sibling xdist workers

_Grouped 2026-09-26; was `absence-asserting-tests-race-under-xdist`, status draft, prio normal._

### Motivation / why

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

### In scope

- Scope both assertions to rows the test itself created — filter
  `_open_drift_alerts` by the model/ref the test upserted; assert the relative
  order of `leaf_a`/`leaf_b` among the test's own todos rather than their
  index in the global doable list.
- Sweep for the same shape elsewhere: assertions of the form `assert not
  <unfiltered list>(store)` or an index comparison against a corpus-wide
  ranked result.

### Explicitly NOT in scope

- Per-test database isolation, or dropping xdist. The fix is a scope
  predicate in the assertion, not a change to the harness.
- The reweighting and drift-detection logic itself — both behaved correctly.

### Acceptance criteria

- Both tests pass under deliberate adversarial concurrency (a parallel worker
  minting a drift alert / an active quest) rather than only in a quiet window.
- No remaining test asserts emptiness of an unscoped corpus-wide query.

### Target + blast radius

`tests/test_llm_catalog.py::TestReconcile`,
`tests/test_quest_reweight.py::TestRotationReweight`. Test-only — no source
change, so no deploy risk.

### Open questions / decisions log

- Is there a reusable fixture-level scope handle (a per-test tag or actor
  slug) already available to filter on, or does each call site need its own
  predicate? Unchecked.

### Second sighting — 2026-09-24, `tests/test_se_block_uid.py`

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

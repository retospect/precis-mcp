---
status: draft
title: test DB — 0093's category-scoped component specs go missing on gate clones
prio: medium
model: sonnet
---

# `component_specs`' category-scoped seed vanishes on the gate's clones

Found 2026-09-05 while shipping se off-the-shelf rung 2b. Cost several
gate cycles to localize, so the evidence is written down in full rather
than summarized.

## Symptom

A `component` series mint reports a **partial** write:

```
specs: 4 written, 0 already current, 4 skipped
       (drive_type; length; thread_pitch; thread_size)
```

The four skipped are exactly migration **0093**'s *category-scoped* core
specs. The four written are **0152**'s *universal* geometry specs. The se
catalog derivation then reports `screw needs length`, three layers away
from the actual fault — which is what made it expensive.

## Where it does and doesn't happen

- **`scripts/test` (dev container, persistent `precis_test`): passes.**
  A direct query of that DB shows all 24 core specs including `length`.
- **`scripts/ship`'s gate: fails**, reproducibly (3 runs). The gate builds
  a co-located tmpfs DB and clones it per xdist worker; the clones are
  dropped at the end of the run, so a post-hoc query of the surviving
  template shows a *healthy* DB and proves nothing. **That mismatch sent
  the first investigation down a false trail** — verify against the DB the
  failing process used, not the one left behind.

## What is ruled out

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

## Why the existing guard doesn't catch it

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

## Current mitigation

`tests/test_se_catalog_binding.py::_ensure_fastener_specs` re-applies
0093 through the store's own connection when the category-scoped tier is
missing, so those tests guarantee their own precondition. `_bolt` still
asserts the mint wrote everything, so a recurrence fails loudly at the
mint instead of surfacing as a catalog error.

## Next step for whoever picks this up

Instrument the clone path, not the template: log
`count(*) FROM component_specs WHERE category_id IS NOT NULL` from inside
a test on a gate clone, and compare against the template at the same
moment. That single number distinguishes "the template was never seeded"
from "the clone diverged from a seeded template", which is the fork the
investigation above could not close from outside the run.

Any test that depends on 0093's category-scoped seed is exposed, not just
se's — this is test-infrastructure, not an se defect.

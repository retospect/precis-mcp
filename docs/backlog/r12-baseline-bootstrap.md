---
title: Restore complete regenerated baseline bootstrap
status: ready
---

R12 release prerequisite, coordinator-reviewed scope. The secrets-count and
PCB correction product commits already passed their independent source reviews.

Premise reproduced in isolated repairs-r12 after canonical `precis db
dump-schema` through 0189: `scripts/test tests/test_schema_baseline.py
tests/test_secret_hint_migration.py -n0 -x` gives five passes then convergence
failure. During seed COPY, `public.precis_kinds_covered_ins()` cannot resolve
`precis_kinds_refresh_guarded()`. pg_dump emits an empty search path; sealed
trigger bodies contain unqualified names. Log: repairs-r12/.scratch/baseline-gate.log.
The earlier vault tests also reported an omitted pgcrypto prerequisite on truly
fresh baseline installs. Verify that second premise with a fresh database.

Repair only baseline generation/bootstrap preparation, with compact rationale
and meaningful full-seed regression. Preserve the numbered migration chain,
seed vocabulary, function ownership/permissions, transaction behavior and
normal post-load search-path expectations. Generate the artifact canonically;
never hand-edit it or change sealed migrations. Do not omit seeds, disable
triggers, or accept a reduced fixture as the release proof.

Acceptance: full canonical generation through 0189; a truly fresh baseline
load without an externally preprovisioned pgcrypto extension; schema/ledger
convergence with numbered replay; exact synthetic vault save/readback and
masked counts; seed preservation. Focused canonical tests, scoped types and
Ruff must pass. Independent source review precedes integration. Coordinator
owns final version/generated release artifact/full gate and deployment.

No production database, credentials, cluster services, engine pins or research
changes. R11 Castor confirmation remains a separate hold.

Implementation: the generator retains the empty pg_dump search path for DDL
and changes only the seed dump preamble to transaction-local
`public, pg_temp`. Triggers and function bodies stay intact; the existing
loader resets the session path after its transaction. The explicit baseline
extension prerequisites now include pgcrypto, already installed by the
numbered chain's sealed0059 migration.

Regression coverage generates every canonical seed table through0189, compares
every emitted seed COPY row/column after load, compares schema and ledger with
numbered replay, and checks the loader's same-session search path. Fresh vault
databases start with only plpgsql: no extension preprovisioning or actors-only
fixture remains. Synthetic save/readback and masked Unicode/multiline counts
cover both the generated and actual release-artifact paths; the sealed-tail
test retains its legacy-data and function ownership/permissions assertions.

Local focused validation uses a canonically generated full baseline temporarily
installed as the current artifact. That large file is excluded from the scoped
fix commit; root must regenerate after cherry-pick before the integrated gate.
Exact commands/results and artifact hash are in
`inbox/r12-baseline-bootstrap-ready.md` in fleet state.

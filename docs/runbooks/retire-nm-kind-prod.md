# Retiring the `nm` kind in prod (nm→se merge)

**When.** Once with the nm→se merge (`docs/backlog/nm-se-merge.md`). The
code side removes nm's entry points, moves its domain layer into
`precis_se.atomic`, renames its one job type `nm_propose` →
`se_propose_atomic`, and ships `precis_se/migrations/0008_se_drop_nm_tables.sql`
which DROPs `nm_blocks`/`nm_ports`/`nm_connects`/`nm_topology`. Everything
below is **prod data**, so it is user-runs-it (an agent prepares, never runs
— `docs/runbooks/prod-one-off-cli.md`).

**Order matters.** Delete the designs BEFORE the drop deploys: after the
deploy there is no `nm` handler, so nothing can render or verify what is
being thrown away.

## 1. Before the deploy — check, then delete the nm designs

Read-only census (safe to run any time):

```
scripts/prod-psql "SELECT ref_id, title, retired_at IS NOT NULL AS retired FROM refs WHERE kind='nm' ORDER BY ref_id"
scripts/prod-psql "SELECT count(*) AS queued_nm_jobs FROM refs WHERE kind='job' AND meta->>'job_type'='nm_propose'"
scripts/prod-psql "SELECT service, host, prio FROM service_config WHERE service IN ('nm_propose','se_propose_atomic')"
```

State when this runbook was written (2026-09-14): three nm designs
(`photonic-arm-3c`, `azo-stick-5nm`, `azo-stick-5nm-cis`), **all already
soft-retired**; zero `nm_propose` job rows; no `service_config` row for
either job-type name. Dogfood deletion is user-sanctioned (merge doc,
"Delete prod nm designs").

Hard-delete the three designs (this is the destructive step — `refs` cascades
to their chunks, tags and relations):

```
scripts/prod-psql "DELETE FROM refs WHERE kind='nm'"
```

Success criterion: the census query above returns `(0 rows)`.

Leaving them retired-but-present is also survivable — a retired ref of an
unregistered kind is unreachable from every read path — but then the rows
outlive the tables their content lived in, which is worse to inherit than an
empty result.

## 2. `service_config` rename (no-op today, run it anyway)

Job types are not gated per name by `service_config` (services are worker
passes), so this row normally does not exist — but an operator may have set a
per-host prio for the job type at some point, and a stale row would silently
govern nothing after the rename:

```
scripts/prod-psql "UPDATE service_config SET service='se_propose_atomic' WHERE service='nm_propose'"
```

`WHERE old-value` makes a repeat run (and the expected zero-row case) a no-op,
the same guard `src/precis/migrations/0148_dispatch_worker_minter_rename.sql`
uses for the same shape of rename.

## 3. Deploy

`scripts/deploy` as usual. The plugin migration drops the four `nm_*` tables;
workers pick up `se_propose_atomic` from the entry points in the deployed
tree (nothing to re-register by hand).

## 4. After the deploy — verify

```
scripts/prod-psql "SELECT table_name FROM information_schema.tables WHERE table_name LIKE 'nm_%' ORDER BY table_name"
scripts/prod-psql "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint WHERE conname='se_blocks_bound_kind_check'"
```

Expected: no `nm_*` tables; the CHECK reads
`bound_kind IN ('cad','structure','component','part')` (no `'nm'`).

Then, from a session against prod: `get(kind='nm')` must fail with the
retired-kind hint naming se atomic mode (`precis.runtime.dispatch`'s
`_RETIRED_KINDS`), and the doctor's next tick must stay green.

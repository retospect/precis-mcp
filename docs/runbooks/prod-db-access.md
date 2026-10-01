# Reading (and carefully writing) the prod DB

**When.** You need rows from `precis_prod` — numbers, a diagnosis, a verify
`SELECT`. `scripts/db` and the `precis-dev` container are **local** only.

## The one-liner

    scripts/prod-psql "SELECT count(*) FROM refs;"     # one-shot
    echo "SELECT …" | scripts/prod-psql                # piped SQL
    scripts/prod-psql                                  # interactive

It hops over ssh to a cluster node (default `caspar`; `PRECIS_PROD_SSH_HOST=
melchior` overrides, `PRECIS_PROD_PSQL_OPTS="-At"` adds psql flags) and runs
`psql` as `agent_rw` against pgbouncer. The pgbouncer address is resolved from
the gitignored overlay by `scripts/lib/pgb-host.sh::resolve_pgb_host`; never
write a literal in docs or scripts.

By hand (what the wrapper does): ssh lands as `deploy` on caspar (already
authorized for the controller), `psql` is at `/opt/homebrew/bin/psql`, and
`~deploy/.pgpass` is populated by the `pgpass` ansible role.

    ssh caspar 'psql -h $PGB_HOST -p 6432 -U agent_rw -d precis_prod -c "SELECT …"'

- Bare `psql -U agent_ro precis_prod` from the deploy shell hits the unix
  socket and fails — you **must** pass `-h <pgbouncer host> -p 6432`.
- Use `agent_rw` for routine reads. `agent_ro` exists but **lacks SELECT on the
  precis tables** (they are owned by `agent_rw`; the default-privileges grant
  did not cover them retroactively) — file a backlog item if it bites.
- Heredocs: escape single quotes the usual way,
  `ssh caspar 'psql … <<SQL ... '\''paper'\'' ... SQL'`.
- `scripts/prod-psql` prints production rows into your context — for large
  result sets prefer a digest query (`count`, `group by`) over raw rows.

⚠ **`agent_rw` is write-capable and this is production.** Keep queries
read-only. Mutate only inside `BEGIN … COMMIT` with a verify `SELECT`, or in a
transaction you can `ROLLBACK`. `agent_rw` holds UPDATE on `refs`, INSERT/
DELETE on `ref_tags`, INSERT on `tags` (verified with `has_table_privilege`).
Worked example — unwedge a stalled recurring job by closing its stuck child:
`DELETE` the child's `STATUS:open` ref_tag, `INSERT` the `STATUS:done` tag link
(look the tag id up in `tags`; ids differ per DB), `DELETE` the
`child-failed:<job>` bubble link. Whether a write needs an ask is governed by
`docs/conventions/thresholds.md`. For a write that needs a `precis` CLI verb,
see [`prod-one-off-cli`](./prod-one-off-cli.md).

## Cluster DB layout

- DB `precis_prod`, schema `public` only. Tables: `refs`, `chunks`,
  `chunk_embeddings`, `chunk_summaries`, `links`, **`ref_events`** (it does
  exist on prod; use its `source/event/payload` columns to diagnose chase
  spin-loops), `worker_logs`, `tags`/`ref_tags`, `cluster_*`, …
- Roles: `agent_rw` (read+write, owner of the precis tables), `agent_ro`
  (read-only — broken for the precis tables, above), `admin` (DDL/grants),
  `cluster_app`, `prometheus_reader`. Defined as `postgres_roles` in the
  overlay's `group_vars/all/main.yml`. (`litellm_app` was dropped 2026-08-06Z
  with the litellm teardown.)
- Schema changes need DDL rights `agent_rw` lacks: use the `deploy` role over
  the local socket on caspar ([`migration-deploy-window`](./migration-deploy-window.md)).
- **Dropping a role — check ownership cross-database.** `DROP ROLE` refuses
  while the role owns *any* object in *any* database on the cluster, but
  `\dn` / owns-objects checks only see the DB you are connected to. A role that
  owns nothing in `precis_prod` can still own whole other DBs
  (`pg_database.datdba`) plus their contents — `DROP DATABASE` those first.
  (Bit the `litellm_app` drop: it owned the `openclaw` and `litellm` DBs.)

## Things that don't work — don't retry

- `scripts/db` / `precis-dev` target a **local** pgvector container (db
  `precis`, user `precis`). Numbers from `scripts/db query …` are local only.
- `~/.secrets/pw/PG_*` files (`PG_PRECIS`, `PG_ADMIN`, `PG_READONLY`,
  `PG_UNIQFILES`) are **local-pgvector** role passwords, not cluster
  credentials; the infra regen script deliberately skips `^PG_`. None work
  against prod.
- The controller's autossh tunnels to caspar's pgbouncer/Postgres ports can be
  up while the controller has **no `psql` installed** and the permission
  classifier blocks reading `~/.pgpass`, so the tunnel is no use directly. ssh
  to caspar (above) bypasses both problems.
- A direct prod `Store` from the controller for exports:
  [`local-prod-draft-export`](./local-prod-draft-export.md).

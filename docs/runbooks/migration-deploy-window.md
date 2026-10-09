# Shipping a schema-touching change (migration deploy window)

**When.** A ship adds or edits a `src/precis/migrations/*.sql`. This is the
protocol, worked end to end on a column-renaming migration (2026-08-30Z).

A migration is **not** a reason to treat a deploy as risky: apply is
automatic, forward-only, ledger-tracked (`public._migrations`) and idempotent.
The traps are around it.

## How migrations reach prod

`scripts/deploy` / `/go` runs `redeploy-precis.yml`, which **applies pending
migrations automatically** — the `precis_web` role task "Apply pending precis
DB migrations" (`deploy/roles/precis_web/tasks/main.yml`) runs `precis migrate`
over the pgbouncer DSN *before* the web daemon comes up, so a
shipped-but-unmigrated DB is caught up on the next deploy. Default is APPLY
(`precis_migrate_dry_run: false`); preview with `-e precis_migrate_dry_run=true`.
So the correct procedure for a feature with a migration is just a deploy (it
migrates, then bounces every `com.precis.*` daemon —
[`cluster-deploy`](./cluster-deploy.md)); no separate manual step.

**Manual fallback** (migrate without a full redeploy): run on caspar as the
`deploy` role over the local socket, which has DDL rights (`agent_rw` does not):

    ssh caspar '/opt/precis/venv/bin/precis migrate \
      --database-url "postgresql://deploy@/precis_prod?host=/tmp"'

Add `--dry-run` to preview. Verify after: `precis migrate --dry-run` →
"nothing to apply". Raw `psql` against prod: [`prod-db-access`](./prod-db-access.md).

## ⚠ No-deploy window: 03:00–~04:20 UTC

A migration deploy that lands in this window hangs ~48 min on `precis_web :
Apply pending precis DB migrations`, then proceeds on its own.

- 03:00Z: caspar (the prod DB node) reboots nightly
  (`daily-update-reboot.sh`; graceful PG drain first).
- 03:30Z: `pg_dump --jobs=4` starts (cron on caspar). It holds **ACCESS SHARE
  on every table** for its whole run, ~48 min (03:30→04:18Z, very stable). An
  `ALTER TABLE … ADD COLUMN` needs **ACCESS EXCLUSIVE**, which conflicts.

**Rule: don't start a migration deploy in that window. Wait it out; don't
abort mid-deploy** — aborting leaves new code installed with migrations
unapplied, strictly worse than a slow deploy. Never cancel the dump.

`scripts/deploy` enforces this (`scripts/lib/backup-window.sh`): inside the
window it refuses a deploy whose target ADDS a `src/precis/migrations/*.sql`
relative to `origin/prod` (computed from git, never by querying prod) and
names the window and its end; a deploy with no pending migration prints a
warning and proceeds. Outside the window it says nothing. If you have checked
on the DB node that the dump is not running, `--ignore-backup-window`
overrides. The bounds and the pg_dump cron slot live together in
`deploy/roles/backups/defaults/main.yml`, so a schedule change moves both;
`DEPLOY_NOW_UTC=HH:MM` fakes the clock to rehearse the message.

*Diagnosis* (the deploy log just goes silent — ansible buffers per task):
`pg_stat_activity` for the waiting pid → `pg_blocking_pids(<pid>)` → blockers
show `application_name='pg_dump'`. `agent_rw` sees
`<insufficient privilege>` for their query text, so confirm from the OS side
with `ps -eo pid,etime,args | grep pg_dump` on caspar. Backup schedule detail:
[`prod-db-backup-and-timezone`](./prod-db-backup-and-timezone.md).

## The window, step by step

1. **Reviewer first** (migrations are a mandatory review), fix findings, then gate.
2. **`scripts/bump <ver>` runs fine from a worktree** (it cds to its own repo
   root; the "run on host" caveat concerns the dev container, not worktrees).
   It needs superuser for `CREATE EXTENSION vector` in a throwaway DB: mint a
   throwaway role on the local stack's Postgres container
   (`CREATE ROLE bumper LOGIN SUPERUSER PASSWORD '<invented>'`), pass it via
   `PRECIS_DATABASE_URL`, drop it after. `pg_dump` 18 against a pg17 server is
   fine (no `\restrict` in output); host `pg_dump` lives at
   `/opt/homebrew/opt/libpq/bin` if not on PATH.
3. **Editing an unshipped migration after the gate ran it:** clear its ledger
   row in the *worktree's own* test-db container (full version string:
   `DELETE FROM public._migrations WHERE version='<NNNN_name>'`), and make the
   migration idempotent (DO-block guards) so re-apply over the already-migrated
   test DB is a no-op.
4. **`ALTER TABLE … RENAME COLUMN` does NOT rename a view's output column** —
   the view needs DROP+CREATE in the same migration. grep views in the
   baseline for the old column before any column rename.
5. **grep `deploy/` and `scripts/` for the old column too** — ansible-embedded
   SQL (e.g. the redeploy drain query) isn't covered by `src/` + `tests/`
   sweeps. The transitional form
   `COALESCE(to_jsonb(r)->>'new', to_jsonb(r)->>'old')` parses on both schemas.
6. **Ship** `scripts/ship --mutate` (full gate). Watch the search-verb 1 KB
   description budget — MCP docstring growth trips `test_token_budget`.
7. **Quiesce:** `ssh <host> 'sudo touch /opt/precis/worker.drain'` on every
   host running workers (melchior, balthazar, caspar, castor, pollux, spark as
   applicable), then poll `STATUS:running` with a live lease down to 0
   (~2 min). Drain stops **claims** only — heartbeats keep running and can hit
   a renamed column in the migrate→bounce gap (bounded, self-heals).
8. **Deploy:** `scripts/deploy` (background; Monitor the announced
   `.deploy-logs` path; set `PRECIS_CATPATH_DIR` per
   [`cluster-deploy`](./cluster-deploy.md)). The first ansible ping can time out
   on a host right after Tailscale starts — `tailscale ping <host>` warms the
   path; re-run. The playbook removes drain flags only on hosts in the bounce
   play's scope: **clean caspar/spark by hand afterwards.**
9. **MCP image:** `scripts/precis-shell --rebuild true` (threads `GH_TOKEN`
   itself). An exit 1 on the trailing TTY attach is fine — check
   `docker image inspect precis-mcp:dev` Created time. Then `/mcp` reconnect.
   (Image lineages: [`dev-image-rebuild`](./dev-image-rebuild.md).)
10. **Verify:** the `_migrations` ledger on prod; venv `direct_url.json`
    `commit_id` on melchior (`/opt/precis/venv`; caspar runs no worker —
    decommissioned 2026-08-14Z); the worker log's first claim cycle.

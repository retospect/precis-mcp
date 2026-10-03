# pgbouncer role

Transaction pooling in front of the prod Postgres. The pool hands a
server connection to a different client after every transaction. So any
session-level state one client creates stays on that server connection
and reaches the next client, unless pgbouncer tracks it.

## `track_extra_parameters = default_transaction_read_only` (gr462726)

On 2026-10-02 a read-only guard, `SET default_transaction_read_only = on`
run through `scripts/prod-psql`, poisoned the shared server connections.
Every `agent_rw` writer then failed with `ReadOnlySqlTransaction` from
22:04Z until the connections were recycled.

**How pgbouncer handles it now.** It keeps a per-client copy of each
tracked parameter. When it links a client to a server connection whose
value differs, it issues `SET <name>=<client value>` first.
- **Tracked by default** (hard-coded in 1.25): `client_encoding`,
  `DateStyle`, `TimeZone`, `standard_conforming_strings`,
  `application_name`.
- **Added by this setting:** `default_transaction_read_only`. Postgres 14
  and later reports it through ParameterStatus, which is the condition
  for tracking.
- **The client that sets it** stays read-only in every later transaction,
  whichever server connection it lands on.
- **Every other client** gets its own `off` put back.
- **Cost:** nothing on the common path. The SET is sent only when the
  values differ.
- **Version note:** prod runs 1.25.1. pgbouncer 1.26.0 (2026-09-23)
  tracks this parameter by default, so drop the line after upgrading.

**Verified** on pgbouncer 1.25.2, a patch release of prod's 1.25.1, from
PGDG, in a throwaway container. Two clients shared one server connection
(`default_pool_size = 1`), and one ran the session SET:

| config | setter afterwards | other client afterwards |
|---|---|---|
| none (today's prod) | `on` | **`on`**: the leak |
| `track_extra_parameters = default_transaction_read_only` | `on` | `off` |

### Rejected: `server_reset_query = RESET default_transaction_read_only` (the first draft)
- **Breaks the setter:** the setter silently becomes writable again from
  its next transaction on.
- **Costs a round trip** on every release.

### Not yet: `server_reset_query = DISCARD ALL` with `server_reset_query_always = 1`
This would clear every kind of leaked session state. It is safe for
prepared statements and TimeZone:
- pgbouncer 1.22 and later drops its record of the server's prepared
  statements on `DISCARD ALL`, and prepares them again when next used;
- tracked parameters are re-applied on the next link.

Two pieces of precis code rely on session state through the pool today,
and `DISCARD ALL` would break them outright:
- **gr463966:** `ingest/claim.py` and `workers/chunk_keywords.py` take
  session advisory locks (`pg_try_advisory_lock`) on autocommit
  connections. The lock and the unlock already land on different backends.
- **gr463967:** asa_bot's `LISTEN` runs through pgbouncer, which does not
  support LISTEN in transaction mode.

Revisit `DISCARD ALL` once both move to transaction-scoped locks or a
direct Postgres DSN. Keep `track_extra_parameters` alongside it. Without
tracking, a client that set itself read-only becomes writable again after
every transaction.

### What else reaches the pool (pg_stat_statements, 2026-09-28 → 10-03, role `agent_rw`)

| statement | calls | verdict |
|---|---|---|
| `SET TIME ZONE 'UTC'` (pool configure hook) | 20,456 | tracked; the server default is already UTC |
| `SELECT pg_try_advisory_lock($1)` / `pg_advisory_unlock($1)` | 2,957 / 2,097 | leaks, gr463966 |
| `SET application_name=…` | ~4,400 | issued by pgbouncer itself: tracking at work |
| `DEALLOCATE ALL` (psycopg) | 537 | handled by pgbouncer 1.22+ |
| `SET default_transaction_read_only = on` | 180 | the gr462726 poison; tracked from this change |
| `LISTEN "precis.cron"` / `"precis.messages"` | 46 / 46 | unsupported in transaction mode, gr463967 |
| `SET hnsw.iterative_scan` / `hnsw.max_scan_tuples` | 1 / 1 | ad-hoc, untracked, not reported by the server; the rule below covers it |

`SET ROLE` cannot be tracked, because `role` is not a reported parameter.
Under transaction pooling it leaks to other clients, which is why
`PRECIS_MCP_DB_ROLE_ENFORCE` must stay off on any pgbouncer DSN
(`store/pool.py::_db_role_enforced`).

**The rule stands for everything untracked:** no session-level `SET`
through the prod DSN. Use `BEGIN READ ONLY`, `SET LOCAL`, or the
`agent_ro` role.

## Acceptance check

There is no new round trip to measure. `SHOW STATS` avg_xact_time would
not have shown one anyway: a reset query runs after the client is
unlinked. After the deploy:
- **Config live:** `SHOW CONFIG` on the admin console lists
  `track_extra_parameters`. Admin read access is td458386. Until then,
  the rendered `pgbouncer.ini` on the DB node shows it; it is readable by
  the pgbouncer user only.
- **No regression:** `SHOW POOLS` `cl_waiting`/`maxwait` and `SHOW STATS`
  `avg_wait_time` stay where they were.
- **Writes work:** a sample of `SHOW default_transaction_read_only`
  through `scripts/prod-psql` reads `off`. Never test by running the SET
  in prod.

**Deploying this restarts pgbouncer.** A template change notifies
`restart pgbouncer` (`launchctl kickstart -k`), which drops every client
connection once. Pools reconnect on their own, and the restart also
recycles any server connection still carrying a leaked setting. Run it
in a restart window.

# Runbook — db-thrash-review (prod index/thrashing cadence)

A recurring pass that asks: *is the prod DB (`precis_prod` on caspar) thrashing —
a runaway query pegging CPU, a missing index forcing repeated full scans, dead
indexes taxing every write, or bloat?* Cadence is **14 days**, enforced
advisory-style by `scripts/db-thrash-review` (surfaced in `/whatneedsdoing`, next
to `token-review`). It exists because the last time nobody watched, an unindexed
GC `DELETE` ran fleet-wide every 2s and pegged caspar for hours before anyone
noticed (fixed: migrations 0077/0078 + `route_log.gc` single-flight).

Tiering (per CLAUDE.md "three tiers"): the *cadence check* is a script (tier 1,
zero model); the *review* is a judgment session (tier 3) — reading `pg_stat_*`
outliers and deciding "index / drop / throttle / leave" needs a model, not a
regex. The script only says **when** it's due; you run the pass.

## When

`scripts/db-thrash-review` prints `db-thrash-review: DUE` when the newest dated
line in `~/.cache/precis/db-thrash-review.log` is >14 days old (or absent).
Inside the window it's quiet. Run the pass when DUE, then
`scripts/db-thrash-review --stamp "<one-line verdict>"` — that resets the clock.

## The pass — prod-hop and run these four scans

Prod-hop as `agent_rw` (read-only; see CLAUDE.md "Peeking at prod"):
`scripts/prod-psql "…"` (it wraps the hop, the pgbouncer coordinates and
the `-X`/pager flags; see `scripts/lib/pgb-host.sh`).

**1. Thrashing now — long-running active queries.** A query with a large
`runtime`, or several copies of the *same* query, is the smoking gun.
```sql
SELECT pid, state, now()-query_start AS runtime, wait_event_type AS wait,
       left(regexp_replace(query, E'\\s+', ' ', 'g'), 80) AS q
FROM pg_stat_activity
WHERE datname='precis_prod' AND state!='idle' AND pid<>pg_backend_pid()
ORDER BY runtime DESC NULLS LAST LIMIT 12;
```

**2. Missing-index candidates — seq-scan-heavy tables.** Read
`avg_rows_per_seqscan` × `seq_scan`: a big table scanned fully, many times, wants
an index. A *small* table (few thousand rows) scanned often is usually the
planner correctly preferring a seq scan — not a missing index; look at the caller
instead (query frequency).
```sql
SELECT relname, seq_scan, idx_scan, seq_tup_read,
       CASE WHEN seq_scan>0 THEN (seq_tup_read/seq_scan) ELSE 0 END AS avg_rows_per_seqscan,
       n_live_tup, pg_size_pretty(pg_total_relation_size(relid)) AS size
FROM pg_stat_user_tables WHERE seq_scan>0 AND n_live_tup>500
ORDER BY seq_tup_read DESC LIMIT 15;
```

**3. Never-used indexes — pure write/space cost.** Check
`pg_stat_database.stats_reset` first: if NULL, `idx_scan=0` means "never used in
the DB's lifetime" (trustworthy); if recently reset, it only means "not lately".
Exclude unique/pk indexes — `idx_scan=0` there can still be enforcing a
constraint. Verify a secondary index against every query on its column(s) before
dropping (a predicate the planner *can't* use — `col IS NULL OR col=$1`, or an
ORDER BY expression that never matches the index expression — explains a live-but-
unused index). Drop via a forward migration.
```sql
SELECT relname, indexrelname, idx_scan,
       pg_size_pretty(pg_relation_size(indexrelid)) AS idx_size
FROM pg_stat_user_indexes
WHERE idx_scan=0 AND pg_relation_size(indexrelid)>1000000
ORDER BY pg_relation_size(indexrelid) DESC LIMIT 12;
```

**4. Dead-tuple bloat / vacuum health.** `dead_pct` >~20% on a hot table, or a
`last_autovacuum` of `∅` on a big churning table, signals vacuum isn't keeping up.
```sql
SELECT relname, n_live_tup, n_dead_tup,
       CASE WHEN n_live_tup>0 THEN round(100.0*n_dead_tup/n_live_tup,1) ELSE 0 END AS dead_pct,
       last_autovacuum
FROM pg_stat_user_tables WHERE n_dead_tup>500 ORDER BY n_dead_tup DESC LIMIT 12;
```

## How to read it

- **Ratio, not absolute.** One table an order of magnitude above its peers is the
  signal, not a big raw number on a big table.
- **Classify each finding → an action:** missing index (add, forward migration) ·
  runaway/unguarded GC (single-flight + batch + index — see `route_log.gc`) ·
  dead index (drop, forward migration) · frequent full-enum of a small table
  (throttle the *caller*, e.g. `corpus_reconcile`'s per-host marker) · bloat
  (investigate autovacuum settings).
- File anything actionable as a `docs/backlog/` item / a gripe; fix in-reach ones directly.
  Watch for growth-without-retention (an insert-only log table with no GC).
- **Counters reset on postmaster restart.** Check `pg_postmaster_start_time()`
  first; counts from a fresh window (a few hours) don't compare to older passes
  and `idx_scan=0` there proves nothing. A new `idx_scan=0` index (fresh ingest)
  needs a second clean window before a drop is filed.
- **Deliberate keeps — don't re-flag:** `chunks_keywords_gin` (backs
  `mode='verbatim'`) and `tag_embeddings_vector_hnsw` (backs ANN below the size
  threshold). A 13-row table (`app_settings`) with huge seq_scan counts means a
  hot caller (`docs/backlog/db-resident-settings.md`), not a missing index.

## Log

The clock is `~/.cache/precis/db-thrash-review.log` (written by
`scripts/db-thrash-review --stamp "<note>"`); entries before 2026-10-30 are in
git history (`git log -p docs/runbooks/db-thrash-review.md`).

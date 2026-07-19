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
line in this file's `## Log` is >14 days old (or absent). Inside the window it's
quiet. Run the pass when DUE, then append a dated line (below) — that resets the
clock.

## The pass — prod-hop and run these four scans

Prod-hop as `agent_rw` (read-only; see CLAUDE.md "Peeking at prod"):
`ssh caspar 'psql -h 100.126.127.107 -p 6432 -U agent_rw -d precis_prod -P pager=off -c "…"'`.

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
- File anything actionable to OPEN-ITEMS / a gripe; fix in-reach ones directly.
  Watch for growth-without-retention (an insert-only log table with no GC).

## Log

Newest first. One line per completed pass — `**YYYY-MM-DD**` + a terse verdict.

- **2026-07-19** — Baseline pass (the review that motivated this runbook).
  Found + fixed: `llm_blob` GC saturating caspar (unindexed hash anti-join, no
  single-flight) → migs 0077 (hash indexes) + `route_log.gc` guards. Dropped ~193
  MB dead indexes (mig 0078: `chunks_dream_score_idx`, `chunks_watch_score_idx`,
  `llm_call_log_source_ts_idx`). Throttled `corpus_reconcile`'s per-tick
  full-enum. Filed: `worker_logs` unbounded → added 30-day sweeper GC. No bloat
  (all dead_pct <5%). Kept `chunks_keywords_gin` (backs `mode='verbatim'`) +
  `tag_embeddings_vector_hnsw` (live, just below the ANN size threshold).

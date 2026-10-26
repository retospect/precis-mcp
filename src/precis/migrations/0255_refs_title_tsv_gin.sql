-- 0255_refs_title_tsv_gin.sql
--
-- GIN expression index for the title lexical legs (gr514956).
--
-- ``refs.title_tsv`` was dropped in v2, so every title leg recomputes
-- ``to_tsvector('english', title_plain_sql(title))`` per row. On prod the
-- unfenced ``_title_match_pointer`` probe (the empty-search "exists under
-- another kind" hint) cost ~1.6 s and now runs capped at 250 ms — most
-- empty searches silently lose the hint. Only ``search_refs_lexical``
-- and ``count_refs_lexical`` benefit: ``find_papers_by_title`` and the
-- ``_cache_ops.py`` query OR the tsvector leg with ``similarity(...)``,
-- which no index serves, so they stay sequential scans.
--
-- The indexed expression must match the query text verbatim — it is the
-- exact SQL ``precis.utils.title_views.title_plain_sql`` emits (tags
-- stripped, ``\cmd`` words and ``$_^{}~`` noise blanked). If that
-- function changes, this index stops matching and must be rebuilt in a
-- new migration.
--
-- Plain CREATE INDEX: Migrator.apply_all() wraps each file in a
-- transaction (CONCURRENTLY cannot run there), and migrations apply in
-- the deploy's single-writer window. refs carries ~517k rows / 882 MB
-- on prod; the plain build holds a SHARE lock, so refs writes are
-- blocked while it builds (tens of seconds), per the 0208 precedent
-- (11.5M rows).
--
-- Forward-only (ADR 0005). Idempotent. Regenerate the baseline snapshot
-- at release time (ADR 0031): `scripts/bump` / `precis db dump-schema`.

BEGIN;

SET LOCAL lock_timeout = '5s';

CREATE INDEX IF NOT EXISTS refs_title_tsv_gin_idx
    ON refs USING gin (
        (to_tsvector('english',
            regexp_replace(regexp_replace(title, '<[^>]+>', ' ', 'g'),
                           '\\[a-zA-Z]+|[$_^{}~]', ' ', 'g')))
    );

COMMIT;

-- End of 0255_refs_title_tsv_gin.sql

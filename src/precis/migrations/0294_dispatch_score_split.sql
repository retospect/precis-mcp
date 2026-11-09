-- 0294_dispatch_score_split.sql
--
-- gr604593: ``refs.prio`` carried two jobs — human priority and machine
-- sequencing. It is human- or filer-set only now (named rungs 1/3/5/8,
-- sparse); sequencing lives on ``meta.dispatch_score`` /
-- ``meta.dispatch.severity`` (see ``precis.utils.dispatch_score``).
--
-- Off-rung values (2,4,6,7,9,10) were machine scores, never judgements:
-- ``gripe_triage``'s band ladder wrote 1/2/4/6/8 and the auto-gripe
-- filers wrote 4/7; the ruling clears "4/6" — read as every non-rung
-- value, since a hand-set rung is the only value worth keeping. Each is
-- converted to ``meta.dispatch_score = 11 - prio`` (same ordering on the
-- higher-is-hotter scale) rather than dropped, so in-flight queues keep
-- their relative order until the live score recomputes over stamped
-- triage inputs.
--
-- Scope: ``kind='gripe'`` rows (both meanings collided there) and
-- ``kind='todo'`` rows inside a plan (the ``plan:<id>`` tag — PR-intake
-- roots carried ``pr_class`` machine prios, groomed fix todos inherited
-- them). Plain todos keep their column: ``prio=2 (cron)`` and hand-set
-- work items are not this axis. Jobs are untouched (their prio is the
-- executor claim queue's own).
--
-- Forward-only (ADR 0005). Idempotent (the UPDATE is a no-op re-run:
-- prio is NULL after the first pass).

BEGIN;

SET LOCAL lock_timeout = '5s';

-- Gripes: off-rung prios were machine scores — preserve their order as
-- meta.dispatch_score and clear the column.
UPDATE refs r
   SET meta = r.meta || jsonb_build_object('dispatch_score', 11 - r.prio),
       prio = NULL
 WHERE r.kind = 'gripe'
   AND r.retired_at IS NULL
   AND r.prio IS NOT NULL
   AND r.prio NOT IN (1, 3, 5, 8);

-- Plan todos (any live todo carrying a ``plan:<id>`` OPEN tag): same
-- conversion — the pr_class intake scores and groomer-inherited prios
-- were sequencing, not judgements.
UPDATE refs r
   SET meta = r.meta || jsonb_build_object('dispatch_score', 11 - r.prio),
       prio = NULL
 WHERE r.kind = 'todo'
   AND r.retired_at IS NULL
   AND r.prio IS NOT NULL
   AND r.prio NOT IN (1, 3, 5, 8)
   AND EXISTS (
         SELECT 1 FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
          WHERE rt.ref_id = r.ref_id
            AND t.namespace = 'OPEN' AND t.value LIKE 'plan:%');

COMMIT;

-- End of 0294_dispatch_score_split.sql

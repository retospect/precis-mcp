-- 0099_alert_meta_columns.sql
--
-- Promote the alert dedup/lifecycle keys from jsonb `meta`
-- (alert_source, fingerprint, resolved_at) to real scalar columns on refs,
-- and rebuild 0030's open-uniqueness index off the columns instead of
-- meta-expressions. This takes refs.meta OUT of the indexed-attribute set,
-- so the per-alert dedup UPDATE (meta || {seen_count,...}) can be a HOT
-- update instead of rewriting all of refs' indexes every nursery pass.
--
-- Additive + nullable (ADR 0005 forward-only): existing rows backfill from
-- meta in this same transaction; meta keys are kept (mirrors 0081/0082).
-- resolved_at is stored in meta as ISO text (alerts.py), so the backfill
-- casts it to timestamptz. The alert open-set is tiny, so the unique index
-- builds sub-second in-txn (cf. 0077's no-CONCURRENTLY rationale).
-- Regenerate the baseline snapshot after merge (ADR 0031): scripts/bump.
--
-- fillfactor on refs: leave headroom so the now-unindexed-meta dedup update
-- can land HOT in-page. Takes full effect on existing pages only after a
-- rewrite (pg_repack, deploy-time op) — new/updated rows adopt it immediately.

BEGIN;

ALTER TABLE refs ADD COLUMN IF NOT EXISTS alert_source TEXT;
ALTER TABLE refs ADD COLUMN IF NOT EXISTS fingerprint  TEXT;
ALTER TABLE refs ADD COLUMN IF NOT EXISTS resolved_at  timestamptz;

UPDATE refs
   SET alert_source = meta->>'alert_source',
       fingerprint  = meta->>'fingerprint',
       resolved_at  = (meta->>'resolved_at')::timestamptz
 WHERE kind = 'alert'
   AND (meta ? 'alert_source' OR meta ? 'fingerprint' OR meta ? 'resolved_at')
   AND alert_source IS NULL AND fingerprint IS NULL AND resolved_at IS NULL;

DROP INDEX IF EXISTS uq_alert_open_source_fingerprint;

CREATE UNIQUE INDEX IF NOT EXISTS uq_alert_open_source_fingerprint
    ON refs (alert_source, fingerprint)
 WHERE kind = 'alert' AND deleted_at IS NULL AND resolved_at IS NULL;

ALTER TABLE refs SET (fillfactor = 85);

COMMIT;

-- End of 0099_alert_meta_columns.sql

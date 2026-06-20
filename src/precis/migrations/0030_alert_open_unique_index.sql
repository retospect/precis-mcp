-- 0030_alert_open_unique_index.sql
--
-- Enforce "at most one OPEN alert per (alert_source, fingerprint)" at the
-- database level. `precis.alerts.raise_alert` dedups by SELECT-then-
-- INSERT, which is *not* atomic: the cluster runs a nursery instance on
-- every node (melchior / caspar / balthazar / spark + the local docker
-- worker), all firing the same per-minute pass, so two instances can
-- both find "no open alert" and both INSERT — leaving duplicate open
-- alerts for one condition (observed: `spin-loop:35299` doubled). The
-- duplicates are cosmetic (they resolve alongside the original) but
-- clutter the /alerts tab and inflate counts.
--
-- The OPEN/resolved lifecycle is primarily a tag (`alert-state:open` vs
-- `alert-state:resolved`), which a unique index can't reference — but
-- `resolve_stale_alerts` also stamps `meta.resolved_at` on every resolve
-- and an open alert never carries it, so `(meta->>'resolved_at') IS NULL`
-- is a faithful column-level proxy for "open". A partial unique index on
-- that predicate gives the invariant; `raise_alert` takes a per-
-- fingerprint advisory lock so the common race serializes instead of
-- tripping the index.
--
-- Existing duplicates would block the unique build, so collapse them
-- first: keep the most-recently-updated open row per (source,
-- fingerprint) and soft-delete the rest (exact redundant copies of a
-- surviving open alert — `deleted_at IS NULL` hides them everywhere,
-- including /alerts and the index predicate).
--
-- Forward-only (ADR 0005). Idempotent: the dedup is a no-op once unique,
-- and the index uses IF NOT EXISTS.

BEGIN;

WITH ranked AS (
    SELECT ref_id,
           row_number() OVER (
               PARTITION BY meta->>'alert_source', meta->>'fingerprint'
               ORDER BY updated_at DESC, ref_id DESC
           ) AS rn
      FROM refs
     WHERE kind = 'alert'
       AND deleted_at IS NULL
       AND (meta->>'resolved_at') IS NULL
)
UPDATE refs
   SET deleted_at = now()
 WHERE ref_id IN (SELECT ref_id FROM ranked WHERE rn > 1);

CREATE UNIQUE INDEX IF NOT EXISTS uq_alert_open_source_fingerprint
    ON refs ((meta->>'alert_source'), (meta->>'fingerprint'))
 WHERE kind = 'alert'
   AND deleted_at IS NULL
   AND (meta->>'resolved_at') IS NULL;

COMMIT;

-- End of 0030_alert_open_unique_index.sql

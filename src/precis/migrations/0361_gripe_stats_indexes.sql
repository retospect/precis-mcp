-- 0361_gripe_stats_indexes.sql
--
-- gr762554: loading /gripes was slow. ``gripe_stats.rolling_stats`` —
-- the page's 1/6/12/24h fixed/verified/duplicates row and the MCP
-- /stats table — computed a per-gripe ``duplicate`` flag whose
-- meta->>'superseded_by' successor probe matched
-- ``keeper.ref_id::text``: no index covers the text form of ref_id, so
-- every outer gripe scanned the whole kind='gripe' set (~N² row visits,
-- each with an int->text cast). The query now probes
-- ``keeper.ref_id::text = ANY(meta, meta-minus-'gr')``; this partial
-- expression index answers it. Retired keepers count as successors, so
-- the index is not narrower than kind='gripe'.
--
-- The same fix pre-narrows the cohort to 24h candidates — a live STATUS
-- attachment or a retirement inside the widest window. That arm rides
-- the ref_tags pkey / ref_tags_tag_id_created_at_idx probes and a
-- column test on the same refs scan, so it needs no index here.
--
-- Forward-only (ADR 0005). Idempotent.

BEGIN;

SET LOCAL lock_timeout = '5s';

CREATE INDEX IF NOT EXISTS refs_gripe_ref_id_text_idx
    ON refs ((ref_id::text))
    WHERE kind = 'gripe';

COMMIT;

-- End of 0361_gripe_stats_indexes.sql

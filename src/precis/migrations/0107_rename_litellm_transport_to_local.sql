-- 0107_rename_litellm_transport_to_local.sql
--
-- The central litellm ``:4000`` proxy this transport used to front is
-- retired; the router's `Transport.LITELLM` enum member is renamed to
-- `Transport.LOCAL` ("litellm" -> "local") so the wire it actually denotes
-- (the served_by-direct/loopback OpenAI-compat path) is named honestly
-- instead of after a decommissioned dependency. This migration rewrites the
-- three places the old string value is embedded in the DB so the code and
-- the data stay in sync:
--
--   (a) the LOAD-BEARING summarizer card's offering (`llm` ref 162070) —
--       its `meta.offerings[].transport` is read back via
--       `Transport(transport_raw)`, so a stale "litellm" would ValueError.
--   (b) the summarizer card's DECORATIVE `summarizers.config.endpoint`
--       (provenance only, never read for routing) — rewritten for
--       coherence.
--   (c) `llm_call_log.transport` history (~87k rows) — keeps the
--       transport group-by (route_log.spend_rollup et al.) clean instead of
--       splitting one wire's spend across two labels.
--
-- Each UPDATE is idempotent: it only touches rows still carrying the old
-- value, so re-running is a no-op.

-- (a) rebuild the offerings array on ref 162070, mapping "litellm" -> "local"
-- element-by-element (only when an offering actually names it — a no-op if
-- already migrated or if the card has no offerings at all).
UPDATE refs
SET meta = jsonb_set(
  meta,
  '{offerings}',
  (
    SELECT jsonb_agg(
      CASE
        WHEN elem->>'transport' = 'litellm'
          THEN jsonb_set(elem, '{transport}', '"local"')
        ELSE elem
      END
    )
    FROM jsonb_array_elements(meta->'offerings') AS elem
  )
)
WHERE kind = 'llm'
  AND ref_id = 162070
  AND meta->'offerings' @> '[{"transport": "litellm"}]'::jsonb;

-- (b) decorative coherence: the summarizer card's provenance endpoint label.
UPDATE summarizers
SET config = jsonb_set(config, '{endpoint}', '"local"')
WHERE name = 'llm-v1' AND config->>'endpoint' = 'litellm';

-- (c) backfill the call-log history so the transport group-by stays clean.
UPDATE public.llm_call_log SET transport = 'local' WHERE transport = 'litellm';

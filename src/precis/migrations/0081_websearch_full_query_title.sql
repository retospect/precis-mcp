-- 0081_websearch_full_query_title.sql
--
-- Backfill full-query titles for Perplexity-tier refs.
--
-- `_title_for_query` used to truncate the ref title to 80 chars ("… "),
-- so the web detail page (which renders `ref.title` verbatim) showed a
-- clipped question — the original query is the whole point of a search
-- ref. Storage now keeps the full query (single line); this heals the
-- rows written under the old truncating handler.
--
-- The untruncated query already lives in `cache_state.meta->>'query'`
-- for every fetched *and* imported row, so we restore from there rather
-- than reconstruct. Whitespace is collapsed to match the handler's
-- `" ".join(query.split())` normalisation. Idempotent: the equality
-- guard makes a re-run a no-op once titles already match.

-- Collapse-then-trim (btrim(regexp_replace(...))) exactly mirrors the
-- handler's `" ".join(query.split())`: split() drops leading/trailing
-- whitespace of every kind *and* collapses internal runs. (Doing btrim
-- first would only strip spaces, leaving a tab/newline for the collapse
-- to turn into a stray edge space.)
UPDATE refs r
   SET title = btrim(regexp_replace(cs.meta->>'query', '\s+', ' ', 'g'))
  FROM cache_state cs
 WHERE cs.ref_id = r.ref_id
   AND r.kind IN ('websearch', 'perplexity-reasoning', 'perplexity-research')
   AND cs.meta ? 'query'
   AND btrim(cs.meta->>'query') <> ''
   AND btrim(regexp_replace(cs.meta->>'query', '\s+', ' ', 'g')) <> r.title;

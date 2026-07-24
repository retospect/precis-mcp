-- 0082_citation_full_claim_title.sql
--
-- Backfill full-claim titles for citation refs.
--
-- The citation handler used to store `refs.title = text.strip()[:200]`,
-- clipping the claim for "list-view scannability". Storage now keeps the
-- whole claim (display truncation moved to `_display_title` in the web
-- layer), so heal the rows written under the old cap.
--
-- The untruncated claim already lives in `refs.meta->>'claim'`, so we
-- restore straight from there — no cross-table join. Only rows whose
-- title actually differs from the stored claim are touched, so a re-run
-- is a no-op. `btrim` guards against a claim that was only whitespace.

UPDATE refs r
   SET title = btrim(r.meta->>'claim')
 WHERE r.kind = 'citation'
   AND r.meta ? 'claim'
   AND btrim(r.meta->>'claim') <> ''
   AND btrim(r.meta->>'claim') <> r.title;

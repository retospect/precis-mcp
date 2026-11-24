-- 0369_trust_chunks_upd_internal_door.sql
--
-- `precis_trust_chunks_upd` (0214) treats every `UPDATE OF ref_id, ord,
-- text, retired_at` on chunks as a live body change and runs the body
-- guard. A body rewrite (`ChunkStore.replace_body_chunk`, gr782951)
-- inserts the replacement at a scratch ord — `chunks_ref_id_ord_key`
-- keeps the old ord occupied until the doomed rows are gone — then
-- settles it at the old ord with a pure ord UPDATE. That settle is
-- bookkeeping, not a body change, but the trigger has no way to know:
-- it re-ran the guard for a move that changes no content, re-stamping
-- the ref and re-checking the write context a second (and third) time
-- for one edit.
--
-- The schema already has the internal door for exactly this shape:
-- `precis.trust_internal`, set transaction-local around a bookkeeping
-- write — the 0214 body guard's own ref re-stamp uses it, 0325's link
-- flip uses it, and the gr782951 edge re-home uses it. Honor it here,
-- narrowly: under the door a chunks UPDATE is exempt only when `ord`
-- is the sole column the guard observes that changed — the rewrite's
-- settle. A door-held write that also moves the chunk's ref, kind,
-- text or retirement is still a body change and still runs the guard
-- (review td784428 — a blanket door would have silenced ref moves and
-- retirement writes too). Only the rewrite's ord settle runs inside
-- it; the INSERT and DELETE triggers stay undoorable (a body
-- appearing or disappearing is always a change).
--
-- Forward-only (ADR 0005): CREATE OR REPLACE, body identical
-- otherwise. Trigger functions take no grants (0214 ships none).

BEGIN;

SET LOCAL lock_timeout = '5s';

CREATE OR REPLACE FUNCTION precis_trust_chunks_upd()
    RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    -- The door exempts only an ord-only bookkeeping move (a body
    -- rewrite's settle, gr782951): every column the guard observes
    -- other than ord must hold — a door-held write moving ref, kind,
    -- text or retirement still faces the guard below.
    IF precis_revision_setting('trust_internal') = 'on'
       AND OLD.ord IS DISTINCT FROM NEW.ord
       AND (OLD.ref_id, OLD.chunk_kind, OLD.text, OLD.retired_at)
           IS NOT DISTINCT FROM
           (NEW.ref_id, NEW.chunk_kind, NEW.text, NEW.retired_at) THEN
        RETURN NULL;
    END IF;
    IF (OLD.ref_id, OLD.ord, OLD.text, OLD.retired_at)
           IS NOT DISTINCT FROM (NEW.ref_id, NEW.ord, NEW.text, NEW.retired_at) THEN
        RETURN NULL;
    END IF;
    PERFORM precis_trust_body_guard(ARRAY(
        SELECT DISTINCT x FROM unnest(ARRAY[
            CASE WHEN precis_trust_subject_chunk(OLD.chunk_kind, OLD.ord)
                      AND OLD.retired_at IS NULL THEN OLD.ref_id END,
            CASE WHEN precis_trust_subject_chunk(NEW.chunk_kind, NEW.ord)
                      AND NEW.retired_at IS NULL THEN NEW.ref_id END]) x
         WHERE x IS NOT NULL));
    RETURN NULL;
END
$$;

COMMIT;

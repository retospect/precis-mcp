-- 0324_scope_sha_excludes_logbook.sql
--
-- gr662069 follow-up (found by the quest lit-search consumer's test): a
-- consideration verdict pins the scope ref's revision as `scope_sha` and
-- counts as valid only while that sha still matches, so a changed question
-- reopens review. 0323 pinned `precis_target_sha('ref', scope)`, whose
-- body hash covers every live `ord >= 0` chunk — on a quest that includes
-- the logbook (`chunk_kind = 'quest_log'`), which every tick appends to.
-- The lit-search step itself writes a logbook entry, so each verdict on a
-- quest went stale on the very tick that consumed it: the hold-out worked
-- once, then every judged-out paper came back.
--
-- `precis_scope_sha` is the same ref sha (title, covered meta, body) with
-- the logbook left out: a quest's log records what was done, it never
-- changes the question. Everything else that moves the question — a
-- retitle, a rubric edit, any non-log body chunk — still moves the pin.
-- The consideration door stamps it and the scoped-validity read compares
-- against it (`precis.store._consideration_ops`); verdicts stamped before
-- this migration never reached a deployment, so nothing is re-pinned.
--
-- Forward-only; idempotent (`CREATE OR REPLACE`).

BEGIN;

CREATE OR REPLACE FUNCTION precis_scope_sha(p_ref_id bigint)
    RETURNS text LANGUAGE sql STABLE AS $$
    SELECT precis_ref_sha_of(r, (
        SELECT md5(coalesce(string_agg(md5(c.text), ',' ORDER BY c.ord, c.chunk_id), ''))
          FROM chunks c
         WHERE c.ref_id = r.ref_id
           AND c.ord >= 0
           AND c.retired_at IS NULL
           AND c.chunk_kind IS DISTINCT FROM 'quest_log'))
      FROM refs r
     WHERE r.ref_id = p_ref_id
$$;

COMMIT;

-- End of 0324_scope_sha_excludes_logbook.sql

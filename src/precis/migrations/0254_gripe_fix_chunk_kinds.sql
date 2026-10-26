-- 0254_gripe_fix_chunk_kinds.sql
--
-- Gripe-fix loop state on the gripe (gr519361, docs/backlog/
-- gripe-fix-loop-on-the-gripe.md). A coordinator's brief, a builder's
-- handoff and a reviewer's verdict become three append-only timeline
-- chunk kinds on the gripe ref — the 0005 `gripe_comment` shape, kept
-- apart from the comment timeline so a 4 KB brief never fills
-- view='comments' and a handoff never blocks the `resolved:` close
-- guard (it reads the newest *comment*).
--
-- precis_trust_subject_chunk (0214, unchanged since) lists the chunk
-- kinds that are another producer's timeline entry rather than the ref's
-- own assertion; "a new chunk kind is part of the subject until it is
-- classified here". The three kinds are appended to that list — a brief
-- on a gripe is the coordinator's note, not a rewrite of the complaint,
-- so it neither needs the owner's context nor re-stamps the trust
-- subject. Same body as 0214 plus the three slugs.
--
-- Forward-only (ADR 0005). Idempotent: ON CONFLICT DO NOTHING and
-- CREATE OR REPLACE.

BEGIN;

INSERT INTO chunk_kinds (slug, is_card, description) VALUES
    ('gripe_brief',   FALSE, 'Gripe-fix brief: the batch brief that assigned this gripe (coordinator, one per round)'),
    ('gripe_handoff', FALSE, 'Gripe-fix handoff: where the builder stopped and what is next'),
    ('gripe_review',  FALSE, 'Gripe-fix review: verdict first line (LAND | FIX) then findings')
ON CONFLICT (slug) DO NOTHING;

CREATE OR REPLACE FUNCTION precis_trust_subject_chunk(p_chunk_kind text, p_ord integer)
    RETURNS boolean LANGUAGE sql IMMUTABLE AS $$
    SELECT p_ord >= 0 AND NOT p_chunk_kind = ANY (ARRAY[
        'gripe_comment', 'todo_comment', 'issue_comment', 'job_event',
        'job_summary', 'job_result', 'run_log', 'quest_log', 'llm_review',
        'figure_turn', 'mermaid_turn', 'tag_overflow',
        'gripe_brief', 'gripe_handoff', 'gripe_review'])
$$;

COMMIT;

-- End of 0254_gripe_fix_chunk_kinds.sql

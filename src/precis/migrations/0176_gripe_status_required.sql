-- 0176_gripe_status_required.sql
--
-- Every gripe carries exactly one STATUS tag, drawn from the gripe
-- vocabulary: open, triaged, ready_for_fix, in_review, done, wontfix.
--
-- Prod drifted: the /gripes badge read "185 live" while 109 were open —
-- 3 gripes carried STATUS:refuted (a *finding* status), and 1 carried no
-- STATUS at all so it appeared in no view. The app-level check
-- (``Tag.parse_strict`` + ``_KIND_STATUS_VALUES``) stops the first path;
-- this migration backfills the existing drift and adds DEFERRABLE
-- INITIALLY DEFERRED constraint triggers so no write path (raw SQL,
-- ``file_gripe_readonly``, a future handler) can reintroduce it.
--
-- Deferred because the legitimate writers pass through a transient bad
-- state: ``add_tag(replace_prefix=True)`` deletes the old STATUS row then
-- inserts the new one, and ``file_gripe_readonly`` inserts the ref before
-- its tag. The check runs once, at COMMIT.
--
-- Forward-only (ADR 0005).

BEGIN;

INSERT INTO tags (namespace, value) VALUES ('STATUS', 'open'), ('STATUS', 'wontfix')
    ON CONFLICT (namespace, value) DO NOTHING;

-- (a1) Off-vocabulary STATUS on a gripe that also has a valid one: drop
-- the stray.
DELETE FROM ref_tags rt
 USING refs r, tags t
 WHERE r.ref_id = rt.ref_id AND r.kind = 'gripe'
   AND t.tag_id = rt.tag_id AND t.namespace = 'STATUS'
   AND t.value NOT IN ('open', 'triaged', 'ready_for_fix', 'in_review',
                       'done', 'wontfix')
   AND EXISTS (
       SELECT 1 FROM ref_tags rt2
         JOIN tags t2 ON t2.tag_id = rt2.tag_id AND t2.namespace = 'STATUS'
        WHERE rt2.ref_id = rt.ref_id AND rt2.tag_id <> rt.tag_id
          AND t2.value IN ('open', 'triaged', 'ready_for_fix', 'in_review',
                           'done', 'wontfix'));

-- (a2) Remaining off-vocabulary STATUS (e.g. the finding status
-- ``refuted``) -> wontfix. Delete the strays, then insert wontfix once per
-- affected gripe: an UPDATE would collide on the (ref_id, tag_id) primary
-- key when a gripe carries two off-vocabulary STATUS tags and no valid one
-- (after (a1), a gripe still holding one has no valid STATUS).
WITH gone AS (
    DELETE FROM ref_tags rt
     USING refs r, tags t
     WHERE r.ref_id = rt.ref_id AND r.kind = 'gripe'
       AND t.tag_id = rt.tag_id AND t.namespace = 'STATUS'
       AND t.value NOT IN ('open', 'triaged', 'ready_for_fix', 'in_review',
                           'done', 'wontfix')
    RETURNING rt.ref_id
)
INSERT INTO ref_tags (ref_id, tag_id, set_by)
SELECT DISTINCT g.ref_id,
       (SELECT tag_id FROM tags WHERE namespace = 'STATUS' AND value = 'wontfix'),
       'agent'
  FROM gone g
ON CONFLICT (ref_id, tag_id) DO NOTHING;

-- (a3) More than one STATUS on a gripe: keep the most recently set.
DELETE FROM ref_tags rt
 USING tags t
 WHERE t.tag_id = rt.tag_id AND t.namespace = 'STATUS'
   AND EXISTS (SELECT 1 FROM refs r
                WHERE r.ref_id = rt.ref_id AND r.kind = 'gripe')
   AND EXISTS (
       SELECT 1 FROM ref_tags rt2
         JOIN tags t2 ON t2.tag_id = rt2.tag_id AND t2.namespace = 'STATUS'
        WHERE rt2.ref_id = rt.ref_id AND rt2.tag_id <> rt.tag_id
          AND (rt2.created_at, rt2.tag_id) > (rt.created_at, rt.tag_id));

-- (a4) Gripes with no STATUS at all -> open.
INSERT INTO ref_tags (ref_id, tag_id, set_by)
SELECT r.ref_id,
       (SELECT tag_id FROM tags WHERE namespace = 'STATUS' AND value = 'open'),
       'agent'
  FROM refs r
 WHERE r.kind = 'gripe'
   AND NOT EXISTS (
       SELECT 1 FROM ref_tags rt
         JOIN tags t ON t.tag_id = rt.tag_id AND t.namespace = 'STATUS'
        WHERE rt.ref_id = r.ref_id);

-- (b) The invariant, checked at commit.
CREATE OR REPLACE FUNCTION public.gripe_status_check() RETURNS trigger
    LANGUAGE plpgsql AS $$
DECLARE
    v_ref_id bigint;
    v_n      integer;
    v_bad    integer;
BEGIN
    IF TG_TABLE_NAME = 'refs' THEN
        v_ref_id := NEW.ref_id;
    ELSIF TG_OP = 'DELETE' THEN
        v_ref_id := OLD.ref_id;
    ELSE
        v_ref_id := NEW.ref_id;
    END IF;

    -- Only gripes that still exist (a cascade delete removes the ref
    -- before this deferred trigger runs).
    IF NOT EXISTS (SELECT 1 FROM refs WHERE ref_id = v_ref_id AND kind = 'gripe') THEN
        RETURN NULL;
    END IF;

    SELECT count(*),
           count(*) FILTER (WHERE t.value NOT IN
               ('open', 'triaged', 'ready_for_fix', 'in_review', 'done', 'wontfix'))
      INTO v_n, v_bad
      FROM ref_tags rt
      JOIN tags t ON t.tag_id = rt.tag_id AND t.namespace = 'STATUS'
     WHERE rt.ref_id = v_ref_id;

    IF v_n <> 1 OR v_bad > 0 THEN
        RAISE EXCEPTION
            'gripe % must have exactly one STATUS tag from '
            '(open, triaged, ready_for_fix, in_review, done, wontfix); '
            'found % STATUS tag(s), % off-vocabulary', v_ref_id, v_n, v_bad
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NULL;
END
$$;

DROP TRIGGER IF EXISTS gripe_status_refs_ins ON refs;
CREATE CONSTRAINT TRIGGER gripe_status_refs_ins
    AFTER INSERT ON refs
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW WHEN (NEW.kind = 'gripe')
    EXECUTE FUNCTION public.gripe_status_check();

DROP TRIGGER IF EXISTS gripe_status_refs_kind ON refs;
CREATE CONSTRAINT TRIGGER gripe_status_refs_kind
    AFTER UPDATE OF kind ON refs
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW WHEN (NEW.kind = 'gripe')
    EXECUTE FUNCTION public.gripe_status_check();

DROP TRIGGER IF EXISTS gripe_status_ref_tags ON ref_tags;
CREATE CONSTRAINT TRIGGER gripe_status_ref_tags
    AFTER INSERT OR UPDATE OR DELETE ON ref_tags
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW
    EXECUTE FUNCTION public.gripe_status_check();

COMMIT;

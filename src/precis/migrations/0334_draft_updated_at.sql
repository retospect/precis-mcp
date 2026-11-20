-- 0334_draft_updated_at.sql
-- Draft readers and modified-order searches use refs.updated_at. Every
-- chunk write logs a chunk_events row — authored edits AND machine ones.
-- Machine (derived) writes — figure data-package refresh, draft_refresh,
-- doctor_tick — mark source.derived=true; the trigger and the repair
-- below exclude marked events so scheduled refreshes and lazy re-renders
-- don't masquerade as authored edits. Keep the rollup transactional.
BEGIN;
SET LOCAL lock_timeout = '5s';

CREATE OR REPLACE FUNCTION touch_draft_from_chunk_event()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    -- Derived (machine) events keep the chunk history but must not
    -- refresh the draft's public "last edited" clock.
    IF (NEW.source->>'derived') IS NOT DISTINCT FROM 'true' THEN
        RETURN NEW;
    END IF;
    UPDATE refs r
       SET updated_at = NEW.ts
      FROM chunks c
     WHERE c.chunk_id = NEW.chunk_id AND r.ref_id = c.ref_id
       AND r.kind = 'draft' AND r.updated_at < NEW.ts;
    RETURN NEW;
END;
$$;

CREATE TRIGGER chunk_events_touch_draft
AFTER INSERT ON chunk_events
FOR EACH ROW EXECUTE FUNCTION touch_draft_from_chunk_event();

-- Historical derived events predate the marker; stamp the identifiable
-- reasons so the repair below excludes them too.
UPDATE chunk_events
   SET source = COALESCE(source, '{}'::jsonb) || '{"derived": true}'::jsonb
 WHERE source->>'reason' IN ('figure-data-package-refresh', 'draft_refresh');

-- Repair existing stale headers from authored event history only —
-- without moving any clock backwards or pretending that migration time
-- was an authored edit.
UPDATE refs r
   SET updated_at = edits.latest
  FROM (
      SELECT c.ref_id, max(e.ts) AS latest
        FROM chunks c JOIN chunk_events e USING (chunk_id)
        JOIN refs d ON d.ref_id = c.ref_id AND d.kind = 'draft'
       WHERE (e.source->>'derived') IS DISTINCT FROM 'true'
       GROUP BY c.ref_id
  ) edits
 WHERE r.ref_id = edits.ref_id AND r.updated_at < edits.latest;

COMMIT;

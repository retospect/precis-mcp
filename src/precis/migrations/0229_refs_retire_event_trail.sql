-- 0229_refs_retire_event_trail.sql
--
-- A soft-delete of a ref left no `ref_events` trail unless the door that
-- issued the UPDATE remembered to `append_event` itself: on main,
-- `store/_refs_ops.py::retire_ref` writes `retired_at = now()` and
-- appends nothing, its dozen-plus callers each may or may not pair an
-- event, `_cad_ops`/`_draft_ops` issue their own raw UPDATEs, and a raw
-- psql UPDATE leaves nothing at all (gr503453: on 2026-08-11 nine job
-- refs were soft-deleted in one transaction with ZERO `ref_events`
-- rows, actor unrecoverable). This trigger makes the trail a property
-- of the TABLE, not of each door: every NULL -> non-NULL `retired_at`
-- transition appends one `('db', 'retired')` row and every non-NULL ->
-- NULL transition one `('db', 'restored')` row — ALL kinds, not only
-- `job`: the cost is one row per soft-delete and the audit gap is
-- generic (the pruned spec scoped job-only; this covers every kind so
-- the next orphan hunt sees the same trail shape for todos, drafts,
-- memories, anything).
--
-- A door that writes its own event still wins: `NOT EXISTS (... AND
-- e.ts = now())` suppresses the trigger's row when a `ref_events` row
-- for that ref already exists in this transaction (`now()` is
-- transaction-stable, and `append_event` never stamps an explicit ts),
-- so a handler that appended its own row earlier in the tx keeps
-- exactly one event — its own, with its own source/payload.
--
-- SECURITY DEFINER so a caller holding UPDATE on refs but no INSERT on
-- ref_events still leaves the trail — without it the trigger's INSERT
-- would fail the whole UPDATE for exactly the raw-write case this
-- exists to catch. `session_user` (not `current_user`) records the
-- actor: under SECURITY DEFINER `current_user` is the function's
-- owner. `search_path` pinned per the vault/carve-out convention
-- (0059/0079).
--
-- Forward-only (ADR 0005). Idempotent (CREATE OR REPLACE + DROP IF
-- EXISTS).

BEGIN;

CREATE OR REPLACE FUNCTION refs_retire_event_trail()
    RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp
AS $$
DECLARE
    ev text;
BEGIN
    IF OLD.retired_at IS NULL AND NEW.retired_at IS NOT NULL THEN
        ev := 'retired';
    ELSIF OLD.retired_at IS NOT NULL AND NEW.retired_at IS NULL THEN
        ev := 'restored';
    ELSE
        RETURN NULL;
    END IF;
    -- One trail row per transition — unless this transaction already
    -- logged one for this ref (a handler's own `append_event`; `now()`
    -- is transaction-stable, so its ts equals this transaction's).
    IF NOT EXISTS (
        SELECT 1 FROM ref_events e
         WHERE e.ref_id = NEW.ref_id AND e.ts = now()
    ) THEN
        INSERT INTO ref_events (ref_id, source, event, payload)
        VALUES (
            NEW.ref_id,
            'db',
            ev,
            jsonb_build_object(
                'kind', NEW.kind,
                'db_user', session_user,
                'application_name',
                    current_setting('application_name', true)
            )
        );
    END IF;
    RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS refs_retire_event_trail ON refs;

CREATE TRIGGER refs_retire_event_trail
    AFTER UPDATE OF retired_at ON refs
    FOR EACH ROW
    WHEN (
        (OLD.retired_at IS NULL AND NEW.retired_at IS NOT NULL)
        OR (OLD.retired_at IS NOT NULL AND NEW.retired_at IS NULL)
    )
    EXECUTE FUNCTION refs_retire_event_trail();

COMMIT;

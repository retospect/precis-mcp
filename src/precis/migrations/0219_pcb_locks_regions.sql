-- 0219_pcb_locks_regions.sql
--
-- docs/backlog/pcb-stackup-regions.md Slice 2: lock and region columns.
-- Storage only; no placer or router reads the new lock columns yet.
--
--   pcb_instances.fixed_side   — the side (layer top|bottom) may not flip.
--   pcb_instances.max_travel_mm — the soft pin (leash, mm; NULL = free).
--                                 Backfilled from meta->'max_travel_mm'
--                                 (a number, or a numeric string, >= 0);
--                                 the meta key is then dropped from every
--                                 row whose column took the value. A row
--                                 whose meta value could not be carried
--                                 keeps it, untouched.
--   pcb_instances.room_id      — the room (a pcb_features row with
--                                 ftype='room') the part is bound to.
--   pcb_instances.group_fixed  — the part's group moves only as a unit.
--   pcb_planes.region          — fill region polygon (NULL = whole board).
--                                 Supersedes region_hint (0138), which no
--                                 writer ever set; copied across here.
--   pcb_planes.priority        — pour order; higher pours first.
--
-- pcb_features.ftype carries no CHECK constraint, so 'room' needs no
-- admission here. Forward-only (ADR 0005). Idempotent. Regenerate the
-- baseline snapshot after merge (ADR 0031): `scripts/bump` /
-- `precis db dump-schema`.

BEGIN;

ALTER TABLE pcb_instances
    ADD COLUMN IF NOT EXISTS fixed_side    boolean NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS max_travel_mm double precision,
    ADD COLUMN IF NOT EXISTS room_id       bigint,
    ADD COLUMN IF NOT EXISTS group_fixed   boolean NOT NULL DEFAULT false;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'pcb_instances_max_travel_mm_check'
    ) THEN
        ALTER TABLE pcb_instances
            ADD CONSTRAINT pcb_instances_max_travel_mm_check
            CHECK (max_travel_mm IS NULL OR max_travel_mm >= 0) NOT VALID;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'pcb_instances_room_id_fkey'
    ) THEN
        ALTER TABLE pcb_instances
            ADD CONSTRAINT pcb_instances_room_id_fkey
            FOREIGN KEY (room_id) REFERENCES pcb_features (feature_id)
            ON DELETE SET NULL NOT VALID;
    END IF;
END $$;

-- Backfill the leash from meta, then drop the meta key. A JSON number or a
-- plain numeric string ("1.5") is carried when >= 0; the CASE keeps the cast
-- from running on anything else. The key is stripped ONLY from rows whose
-- column is now non-NULL; anything not carried stays in meta untouched.
UPDATE pcb_instances
   SET max_travel_mm = CASE
         WHEN jsonb_typeof(meta->'max_travel_mm') = 'number'
           OR (jsonb_typeof(meta->'max_travel_mm') = 'string'
               AND (meta->>'max_travel_mm') ~ '^\s*[0-9]+(\.[0-9]+)?\s*$')
         THEN CASE
                WHEN (meta->>'max_travel_mm')::double precision >= 0
                THEN (meta->>'max_travel_mm')::double precision
              END
       END
 WHERE max_travel_mm IS NULL AND meta ? 'max_travel_mm';
UPDATE pcb_instances
   SET meta = meta - 'max_travel_mm'
 WHERE meta ? 'max_travel_mm' AND max_travel_mm IS NOT NULL;

CREATE INDEX IF NOT EXISTS pcb_instances_room_id_fk_idx
    ON pcb_instances (room_id) WHERE room_id IS NOT NULL;

COMMENT ON COLUMN pcb_instances.fixed_side IS
    'Side lock (pcb-stackup-regions Slice 2): true = layer may not flip.';
COMMENT ON COLUMN pcb_instances.max_travel_mm IS
    'Soft pin: mm a placer may move the part from its stored pose; NULL = free.';
COMMENT ON COLUMN pcb_instances.room_id IS
    'Room binding: a pcb_features row with ftype=''room'' on the same design.';
COMMENT ON COLUMN pcb_instances.group_fixed IS
    'The part''s group moves only as a unit, never member by member.';

ALTER TABLE pcb_planes
    ADD COLUMN IF NOT EXISTS region   jsonb,
    ADD COLUMN IF NOT EXISTS priority integer NOT NULL DEFAULT 0;

UPDATE pcb_planes
   SET region = region_hint
 WHERE region IS NULL AND region_hint IS NOT NULL;

COMMENT ON COLUMN pcb_planes.region IS
    'Fill region polygon (mm); NULL = whole board. Supersedes region_hint.';
COMMENT ON COLUMN pcb_planes.priority IS
    'Pour priority: higher pours first, lower is cleared around it.';
COMMENT ON COLUMN pcb_planes.region_hint IS
    'Superseded by region (0219); kept for forward-only history, not written.';

COMMIT;

-- Validate outside the DDL transaction (squawk constraint-missing-not-valid,
-- adding-foreign-key-constraint): SHARE UPDATE EXCLUSIVE, no write block.
-- Every room_id is NULL and every backfilled leash is >= 0, so neither fails.
BEGIN;
ALTER TABLE pcb_instances VALIDATE CONSTRAINT pcb_instances_max_travel_mm_check;
ALTER TABLE pcb_instances VALIDATE CONSTRAINT pcb_instances_room_id_fkey;
COMMIT;

-- End of 0219_pcb_locks_regions.sql

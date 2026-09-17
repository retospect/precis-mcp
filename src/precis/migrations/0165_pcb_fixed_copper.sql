-- 0165_pcb_fixed_copper.sql
--
-- docs/backlog/pcb-pre-place-route-blocks.md Slice 1 — the storage seam
-- for "generators emit real fixed copper (vias + traces), solved once per
-- unit cell and tiled" (Reto, 2026-09-15: "fixed copper ... seem right ...
-- there automatically, with all the spacing"). Today a generator's escape
-- fabric is fake — every neck stub and plaza via rides as a FOOTPRINT PAD
-- row, invisible to the router, refused by ``view='gerber'``, and
-- unreadable to DRC as copper (the spec's "geometry that lies" section).
-- This migration does not fix any of that yet — no generator emits copper
-- as of this migration — it only opens the seam a later slice fills.
--
-- **``pcb_fixed_copper`` is authored copper, an INPUT parallel to
-- ``pcb_planes``** (0138's own precedent: "authored plane assignment per
-- (board, layer, net)"), never a second writer of ``pcb_copper``.
-- ``pcb_copper`` stays exactly what 0138 already says it is — "DERIVED
-- ... regenerated wholesale (DELETE board's rows + INSERT) per realize
-- run ... never hand-edited, no retired_at" — a generator writing into it
-- directly would have its own fabric deleted by the next realize run.
-- Unlike ``pcb_copper``, ``pcb_fixed_copper`` DOES soft-delete
-- (``retired_at``): a generator re-apply with changed params retires
-- exactly its own rows (scoped by ``ref_id``/``generator_name``, the same
-- identity ``pcb_generators`` already keys on) and re-inserts fresh, the
-- same discipline ``_pcb_generator_retire_expansion`` already applies to
-- a generator's components/nets/connections/features
-- (``precis.store._pcb_ops``).
--
-- ``layer`` is ``text`` (a layer NAME, e.g. ``'F.Cu'``), matching
-- ``pcb_copper.layer`` exactly rather than an integer index — the two
-- tables' rows are unioned read-back (``pcb_copper_list``) with zero
-- reshaping, so a type mismatch there would be a bug at every reader
-- (DRC/gerber/SVG), not a simplification. For a ``via`` row the column
-- holds the same schema-satisfying placeholder ``pcb_copper``'s own via
-- rows already use (0138's writer, ``precis.workers.job_types.
-- pcb_route``): a via's real layer membership lives in
-- ``geom['span']`` (the two layer names it flashes between), never in
-- this column.
--
-- ``envelope`` records the rule envelope (layer count, clearance/track/
-- via floor) the fabric was solved under — the spec's hard gate: "the
-- fabric records the stackup / layer count / clearance rules it was
-- solved under. Re-applying into a design whose rules moved refuses
-- honestly rather than silently keeping copper that is no longer legal."
-- An empty ``envelope`` (the default) is never checked — only a row that
-- actually records one can be refused over one.
--
-- ``net_id`` is nullable (unlike ``pcb_copper.net_id``): some authored
-- fixed geometry may be netless at write time; the store layer
-- (``pcb_fixed_copper_put``) still refuses a row naming an unknown net.
--
-- Forward-only (ADR 0005). Idempotent. Regenerate the baseline snapshot
-- after merge (ADR 0031): `scripts/bump` / `precis db dump-schema`.

BEGIN;

CREATE TABLE IF NOT EXISTS pcb_fixed_copper (
    fixed_id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    board_id           bigint NOT NULL REFERENCES pcb_boards (board_id) ON DELETE CASCADE,
    ref_id             bigint NOT NULL REFERENCES refs (ref_id) ON DELETE CASCADE,
    -- pcb_generators.name -- the generator CALL that emitted this row (its
    -- design-local identity), never the generator TYPE below.
    generator_name     text   NOT NULL,
    generator          text   NOT NULL,           -- generator TYPE, e.g. ewod_pad_array
    generator_version  text,
    ctype              text   NOT NULL,            -- track | via
    layer              text   NOT NULL,            -- layer NAME, matches pcb_copper.layer
    net_id             bigint REFERENCES pcb_nets (net_id) ON DELETE CASCADE,
    -- SAME shape pcb_copper.geom already uses per ctype (track: segments/
    -- length_mm/width_mm/is_dogbone; via: x/y/dia_mm/drill_mm/span), mm.
    geom               jsonb  NOT NULL,
    -- the rule envelope the fabric was solved under (layers/min_clearance_
    -- mm/min_track_mm/via_drill_mm/via_diameter_mm, whichever the emitter
    -- records) -- checked against the board's current stackup/rule floor
    -- at apply time (precis.store._pcb_ops's envelope gate); empty = not
    -- checked.
    envelope           jsonb  NOT NULL DEFAULT '{}',
    meta               jsonb  NOT NULL DEFAULT '{}',
    created_at         timestamptz NOT NULL DEFAULT now(),
    retired_at         timestamptz,
    CONSTRAINT pcb_fixed_copper_ctype_chk CHECK (
        ctype = ANY (ARRAY['track'::text, 'via'::text])
    )
);

COMMENT ON TABLE pcb_fixed_copper IS
    'AUTHORED copper (pcb-pre-place-route-blocks Slice 1), an INPUT '
    'parallel to pcb_planes -- never a second writer of pcb_copper, which '
    'stays exactly the DERIVED, wholesale-regenerated table 0138 already '
    'defines. A generator re-apply with changed params soft-deletes '
    '(retired_at) exactly its own rows, scoped by (ref_id, '
    'generator_name), and re-inserts fresh -- the same identity discipline '
    'pcb_generators already keys idempotency on. A realize run seeds '
    'pcb_copper from this table''s active rows as pre-existing fixed '
    'segments and routes only what remains.';

CREATE INDEX IF NOT EXISTS pcb_fixed_copper_board_idx
    ON pcb_fixed_copper (board_id) WHERE retired_at IS NULL;
CREATE INDEX IF NOT EXISTS pcb_fixed_copper_ref_gen_idx
    ON pcb_fixed_copper (ref_id, generator_name) WHERE retired_at IS NULL;
-- the retired_at-filtered partials above don't cover an unqualified
-- FK-cascade lookup (test_schema_design.py convention, 0136/0138) -- a
-- plain index alongside each covers it.
CREATE INDEX IF NOT EXISTS pcb_fixed_copper_board_id_fk_idx
    ON pcb_fixed_copper (board_id);
CREATE INDEX IF NOT EXISTS pcb_fixed_copper_ref_id_fk_idx
    ON pcb_fixed_copper (ref_id);
CREATE INDEX IF NOT EXISTS pcb_fixed_copper_net_id_fk_idx
    ON pcb_fixed_copper (net_id);

COMMIT;

-- End of 0165_pcb_fixed_copper.sql

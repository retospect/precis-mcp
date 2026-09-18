-- 0169_design_revisions.sql
--
-- (0167 and 0168 are held by sibling trees' migrations landing now.) The
-- design revision RECORD (the design-workbench build, slice 2 (2026-09-18)):
-- one row per saved version of a design, carrying the op list that
-- produced it, so a scrubber can show "what changed at N" rather than only
-- "what the design looks like now".
--
-- Two renters, two snapshot strategies, ONE ledger:
--
--   * `structure` versions its atoms in rows already (`struct_atoms.
--     added_version` / `retired_version`, migration 0042) — scene-at-N is
--     a row filter, so its revision rows carry `checkpoint_id` NULL and
--     `rev` equals `refs.meta.version`.
--   * `se` persists by retire-all/reinsert-all with no versions, so each
--     save's snapshot is a `design_checkpoints` row (label `rev-<N>`) and
--     `checkpoint_id` points at it. Reuses the existing checkpoint store
--     on purpose — a second snapshot table would be the same payload
--     column twice, and "revert to N" is then `load_checkpoint` +
--     `save_tree`, nothing new.
--
-- `turn` is the chat turn that produced the ops (slice 3's tool-less
-- L0/L1 loop); NULL for a direct `edit`/`put`. `ops` is jsonb because it is
-- the verbatim op list the handler was given — the schema of an op is the
-- kind's own vocabulary (`precis_se.ops.known_ops`, the structure op
-- table), and core stores the list opaquely, exactly as it does the
-- checkpoint payload.
--
-- Pre-migration designs have no rows here; the reader shows them as one
-- revision ("current, no record") rather than backfilling a history
-- nobody recorded.
--
-- Forward-only (ADR 0005). Idempotent.

BEGIN;

CREATE TABLE IF NOT EXISTS design_revisions (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ref_id        bigint NOT NULL REFERENCES refs (ref_id) ON DELETE CASCADE,
    rev           int NOT NULL CHECK (rev > 0),
    -- The op list that produced this revision, verbatim. `[]` for a
    -- whole-tree `put` (the tree is the snapshot; there was no delta).
    ops           jsonb NOT NULL DEFAULT '[]'::jsonb,
    turn          text,
    -- The snapshot, for renters that do not version in rows. SET NULL,
    -- not CASCADE: deleting a checkpoint loses the snapshot, not the fact
    -- that the revision happened or the ops that made it.
    checkpoint_id bigint REFERENCES design_checkpoints (id) ON DELETE SET NULL,
    created_at    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (ref_id, rev)
);

COMMENT ON TABLE design_revisions IS
    'One row per saved version of a design: the ops that produced it and, '
    'for renters that do not version in rows (se), the design_checkpoints '
    'snapshot taken at that save. rev is the renter''s own version number '
    '(structure: refs.meta.version; se: 1 + prior row count). turn is the '
    'chat turn that produced the ops, NULL for a direct edit/put.';

CREATE INDEX IF NOT EXISTS design_revisions_checkpoint_idx
    ON design_revisions (checkpoint_id);

COMMIT;

-- End of 0169_design_revisions.sql

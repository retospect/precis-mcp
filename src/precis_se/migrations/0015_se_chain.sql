-- precis_se/0015_se_chain.sql
--
-- The nucleic-acid (chain) domain leg for `se`
-- (docs/backlog/se-nucleic-acid.md slice 1; the physics/arithmetic is the
-- `precis_chain` kernel, the vocabulary is :mod:`precis_se.chain`): a
-- block may now BE a helix or a strand, and a strand's route across
-- helices is a list of `domain` rows on the existing topology table.
-- Two payloads, both vetted by :mod:`precis_se.chain.vocab` at write
-- time — this migration only opens the storage, never the shape rules:
--
-- * `se_blocks.chain` — the per-block chain declaration. A COLUMN, not a
--   table, for exactly the reason `dof` and `chromophore` already are
--   (0007/0010): one per block, meaningless without the block, gone the
--   moment the block goes. Three roles live in it, keyed by `role`:
--   - `helix`  — carries the GEOMETRY (scadnano's decomposition, chosen
--     2026-09-27): `{role, motif, nucleic, path:{waypoints_m |
--     lattice:{kind,row,col}}, n_units, phase0, register:{lattice},
--     min_bend_radius_m?, min_gap_m?}`.
--   - `strand` — carries the ROUTE's chemistry: `{role, sequence?,
--     nucleic}`. The route itself is the domain rows below.
--   - `segment` — a `layout_chain` child (`<helix>.s<k>`): `{role,
--     helix, ord, start, end}`, the realizer seam — the `[start, end]`
--     unit range this child's envelope covers, with the ranges of one
--     helix's children tiling it exactly.
--   Lengths are metres and angles radians, like every other se number
--   (the `_m` suffix marks the ones whose key would otherwise read as
--   unit-less).
--
-- * `se_topology.kind` grows `'domain'` — one ordered domain of a
--   strand's route: `subject_name` is the STRAND block, `object_name`
--   the HELIX block, and `meta` carries `{ord, forward, start, end,
--   geometry?, overrides?, loop_before_nt?}`. It belongs on this table
--   rather than a new one for the reason 0007 gave `threading`: it is
--   one directional, name-keyed L2 fact per row about a pair of blocks,
--   retired and reinserted in the same pass. Unlike `threading`, a
--   domain row is ORDERED within its strand (`meta.ord` is the 5'→3'
--   index), and that ordinal — not the block pair — is what identifies
--   it: one strand crosses the same helix twice in any real origami, so
--   `se_topology_threading_pair_key`'s (ref_id, subject, object) shape
--   is not unique here. Hence a second partial unique index keyed on
--   (ref_id, subject_name, (meta->>'ord')) instead.
--
-- Zero live rows of either in prod as of this migration.
--
-- The migration bodies get `BEGIN;`/`COMMIT;` stripped and executed
-- whole by the plugin-migration test fixtures (0009's header), so every
-- statement below stays independently runnable.
--
-- Forward-only (ADR 0005). Idempotent. This is a PLUGIN migration
-- (namespace `precis_se`), applied after 0001-0014.

BEGIN;

-- 1. the block-owned chain declaration -------------------------------------
ALTER TABLE se_blocks ADD COLUMN IF NOT EXISTS chain jsonb;

COMMENT ON COLUMN se_blocks.chain IS
    'The per-block nucleic-acid declaration (precis_se.chain.vocab.'
    'validate_chain): role=helix carries the geometry (motif, path or '
    'lattice site, n_units, phase0, register, min_bend_radius_m?, '
    'min_gap_m?), role=strand the route''s chemistry (sequence?, '
    'nucleic), role=segment a layout_chain child''s [start, end] unit '
    'range. Block-owned like dof/chromophore, not a table of its own — '
    'one per block, meaningless without it, gone when it goes. NULL = '
    'this block is not part of a chain. Metres/radians.';

-- 2. se_topology grows the strand's ordered route --------------------------
ALTER TABLE se_topology DROP CONSTRAINT IF EXISTS se_topology_kind_check;
ALTER TABLE se_topology ADD CONSTRAINT se_topology_kind_check
    CHECK (kind IN ('threading', 'domain')) NOT VALID;

COMMENT ON TABLE se_topology IS
    'se L2 topology invariants, one row per fact. kind=''threading'' '
    '(0007, atomic mode) means subject_name is threaded through '
    'object_name — directional, name-keyed, never re-derived from '
    'geometry. kind=''domain'' (0015, the nucleic-acid domain) is one '
    'ordered stretch of a STRAND (subject_name) along a HELIX '
    '(object_name), with meta={ord, forward, start, end, geometry?, '
    'overrides?, loop_before_nt?}; a strand''s domains in ord order ARE '
    'its 5''→3'' route, and pairing is derived from co-occupancy of a '
    'helix offset (precis_se.chain.pairing), never declared.';

-- Identity of a domain row is (strand, ord) — NOT the block pair the
-- threading index keys on: one strand crosses the same helix twice in
-- any real origami, so the pair repeats by design while the ordinal
-- cannot.
CREATE UNIQUE INDEX IF NOT EXISTS se_topology_domain_ord_key
    ON se_topology (ref_id, subject_name, (meta->>'ord'))
    WHERE retired_at IS NULL AND kind = 'domain';

COMMIT;

-- End of 0015_se_chain.sql

-- precis_se/0010_se_fret.sql
--
-- FRET (optical-link) domain leg for `se` (the package docstring's
-- "optical domain" paragraph is the map; what round 1 deliberately left
-- out is docs/backlog/se-fret-round-2.md, and the physics itself is
-- :mod:`precis_se.fret`): Förster resonance energy transfer as a new L2
-- physics on the block tree, alongside the existing joint/atomic-bond
-- legs. Three payloads, all vetted by :mod:`precis_se.fret`'s validators
-- at write time (`validate_chromophore`/`validate_optical_link`/
-- `validate_optics`) — this migration only opens the storage, never the
-- shape rules:
--
-- * `se_blocks.chromophore` — the per-block optical property card
--   (transition dipole in the block frame, quantum yield, lifetime,
--   emission/absorption spectra). A COLUMN, not a table, for exactly the
--   reason `dof` and `objectives` already are: one per block, meaningless
--   without the block, gone the moment the block goes. There is no
--   "chromophore lives independently of its block" case the way there is
--   for, say, a BOM line naming several blocks.
--
-- * `se_connects.optical` — the declared L2 invariant on a pair
--   (`{'min_efficiency', 'channel'?, 'reason'?}`). Sits beside `joint`
--   on the very same table for the very same reason: it is a fact about
--   one edge, keyed the same way, retired and reinserted in the same
--   pass. Deliberately NOT exclusive with `joint`/`kind` — an optical
--   link is a different physics on the same pair, not a competing claim
--   about the same one (see the column's own comment on `precis_se.ops.
--   ConnectSpec.optical`).
--
-- * `se_optics` — the design's optical context (medium refractive index,
--   optional excitation wavelength). This one DOES get its own table,
--   for the opposite reason the other two don't: it is a fact about the
--   *design*, and unlike a block or a connect, a design has no existing
--   row of its own to hang a column off — `refs` is core's, shared by
--   every kind, and se does not get to add kind-specific columns there.
--   `se_optics` is therefore a one-row-per-live-design table, the same
--   shape `se_notes` and the rest of the plugin's tree tables already
--   are, just with a partial-unique index enforcing the "one" instead of
--   letting `ref_id` repeat: the whole design's optical context is a
--   single record, not a ledger, so there is exactly one
--   live row or none, and `persist.save_tree` retires the live row and
--   reinserts a fresh one only when `tree.optics` is not `None` — the
--   retire-all/reinsert-all discipline every se table already follows,
--   applied to a table of size 0 or 1.
--
-- Zero live rows of any of these in prod as of this migration.
--
-- The migration bodies get `BEGIN;`/`COMMIT;` stripped and executed
-- whole by the plugin-migration test fixtures (0009's header), so every
-- statement below stays independently runnable.
--
-- Forward-only (ADR 0005). Idempotent. This is a PLUGIN migration
-- (namespace `precis_se`), applied after 0001-0009.

BEGIN;

-- 1. the block-owned optical property card ----------------------------------
ALTER TABLE se_blocks ADD COLUMN IF NOT EXISTS chromophore jsonb;

COMMENT ON COLUMN se_blocks.chromophore IS
    'The per-block optical property card (precis_se.fret.'
    'validate_chromophore): {label, dipole (block-frame [x,y,z]), '
    'quantum_yield, lifetime_s, emission, absorption}. Block-owned like '
    'dof/objectives, not a table of its own — one per block, meaningless '
    'without it, gone when it goes. NULL = this block is not a FRET node.';

-- 2. the connect-owned optical L2 invariant ---------------------------------
ALTER TABLE se_connects ADD COLUMN IF NOT EXISTS optical jsonb;

COMMENT ON COLUMN se_connects.optical IS
    'The declared optical (FRET) L2 invariant on this pair '
    '(precis_se.fret.validate_optical_link): {min_efficiency, channel?, '
    'reason?}. Deliberately compatible with joint/kind on the same row — '
    'an optical link is a different physics on the same pair, not a '
    'competing claim about the same one, unlike joint vs kind.';

-- 3. the design-level optical context ---------------------------------------
CREATE TABLE IF NOT EXISTS se_optics (
    id             bigserial PRIMARY KEY,
    ref_id         bigint NOT NULL REFERENCES refs (ref_id) ON DELETE CASCADE,
    medium_index   double precision NOT NULL,
    excitation_nm  double precision,
    retired_at     timestamptz,
    created_at     timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE se_optics IS
    'A design''s optical context (precis_se.fret.validate_optics): the '
    'refractive index every Forster radius in the design divides by, and '
    'the pump wavelength every spectral-crosstalk figure is quoted at. '
    'One live row per design (se_optics_ref_live_key), never a ledger — '
    'persist.save_tree retires the live row and reinserts a fresh one '
    'only when tree.optics is not None, the same retire-all/reinsert-all '
    'discipline the rest of this plugin''s tables follow.';

-- One live row per design.
CREATE UNIQUE INDEX IF NOT EXISTS se_optics_ref_live_key
    ON se_optics (ref_id) WHERE retired_at IS NULL;
-- Plain (non-partial): the 0001 rule — a retired_at-partial index doesn't
-- serve the FK-cascade scan `ref_id = $1` over retired rows too.
CREATE INDEX IF NOT EXISTS se_optics_ref_idx ON se_optics (ref_id);

COMMIT;

-- End of 0010_se_fret.sql

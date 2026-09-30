-- precis_se/0017_se_optics_channels.sql
--
-- `se_optics.channels_available` — the design's spectral channel budget:
-- how many light (or reagent) channels this design's sources can address
-- independently (docs/backlog/photoswitch-states-and-spectral-dof.md,
-- "the number of orthogonal channels as a scarce budget the designer
-- spends"; built for se-walker-light-protocol, whose walker is the first
-- design that spends several at once).
--
-- A third column on the design's optical context (0010's `se_optics`),
-- not a table: it is one integer about the space the design sits in,
-- exactly like `medium_index`, declared through the same `set_optics` op
-- and vetted by the same `precis_se.fret.validate_optics`. NULL = no
-- budget authored, and `precis_se.chain.spectral` then emits no
-- `chain_channel_budget` row — a budget is a declared constraint, never an
-- assumed one.
--
-- The migration bodies get `BEGIN;`/`COMMIT;` stripped and executed
-- inside the caller's transaction (precis_se/migrations/__init__.py).

BEGIN;

ALTER TABLE se_optics
    ADD COLUMN IF NOT EXISTS channels_available integer
        CHECK (channels_available IS NULL OR channels_available >= 1);

COMMENT ON COLUMN se_optics.channels_available IS
    'spectral channel budget: independently addressable light/reagent channels (NULL = none authored)';

COMMIT;

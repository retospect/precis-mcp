-- precis_se/0006_units_se_pose_rot_rad.sql
--
-- units-policy-cutover angle ruling (docs/backlog/units-policy-cutover.md
-- decisions log, "se pose_rot deg->rad migration"): `se_blocks.pose_rot`
-- has held degrees since 0001; the shared `precis.cad.vec.rotation`/
-- `pose` constructor (round 8, same deploy as this migration) now takes
-- radians, so every stored `pose_rot` must convert in lockstep or a live
-- design's rotation silently reinterprets by a factor of ~57.3. This is
-- a LOSSLESS numeric rewrite, not the clean-slate wipe pattern
-- `0159_units_cad_wipe.sql`/`precis_nm/migrations/0007_units_nm_wipe.sql`
-- used for cad/nm's length cutover — se's stored designs
-- (unicycle-printed-v1, boxel-3nm) are live dogfood, not throwaway test
-- data ("se designs are untouched" meant no wipe, not no rewrite).
--
-- `radians(x) = x * pi() / 180` is exact float64 arithmetic (same
-- precision class as the nm Å->m ×1e-10 rewrite this mirrors) — a
-- migration test seeds a degree row (0, negative, >360) and asserts the
-- exact `radians()` output.
--
-- Applies element-wise over the whole `pose_rot` array regardless of its
-- length (the column is `NOT NULL DEFAULT '{0,0,0}'`, always 3 elements
-- in practice, but this doesn't assume that) via `ARRAY(SELECT ... FROM
-- unnest(...))`, which preserves order and — unlike `array_agg` — maps a
-- zero-length input to `{}` rather than `NULL` (`NOT NULL` guard).
--
-- Forward-only (ADR 0005). NOT idempotent by nature (running this twice
-- would double-convert, `radians(radians(x)) != x`) — like any other
-- numeric-rewrite migration, it is tracked by the migration ledger and
-- runs exactly once per database. Plugin migration (namespace
-- `precis_se`), applied after core via `Migrator.discover_sources`.

BEGIN;

UPDATE se_blocks
SET pose_rot = ARRAY(SELECT radians(x) FROM unnest(pose_rot) AS t(x))
WHERE pose_rot IS NOT NULL;

COMMIT;

-- End of 0006_units_se_pose_rot_rad.sql
